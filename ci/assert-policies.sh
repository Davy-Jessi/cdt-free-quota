#!/bin/sh
# CI 入口：跑所有静态断言。
#
# 用 sh 而不是依赖具体 CI 平台 —— 本地、GitHub Actions、GitLab CI 都能直接调。
#
# 当前 5 步：策略断言 / 凭据形态 / 泄漏闸 / %-格式化 / 未定义名。
# ⚠️ 步骤编号与 ci.yml、README、docs/pre-commit-checklist.md 里写的「几步」必须一致 ——
#    早期版本这里是 4 步，而 README 把「未定义名」也列成 ci/ 的检查项，
#    实际那一项**只在部署脚本里跑**，仓库自身的 CI 从来不看它。
#    「写了检查」和「每次 push 都会跑」是两件事。
set -e
cd "$(dirname "$0")/.."

# ⚠️ 不要写死 python3。
#    某些系统上 `python3` 是个占位符可执行文件：`command -v` 找得到，
#    但执行时静默失败（无输出、无报错）。踩过 —— CI 脚本调它什么也不打印。
#    所以逐个试，并且**要求解释器真的能打印一个标记**才算可用。
PY=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1 && [ "$("$c" -c 'print(1)' 2>/dev/null)" = "1" ]; then
    PY="$c"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "X 找不到可用的 Python 解释器（某些系统上 python3 是个占位符可执行文件）"
  exit 1
fi
echo "  使用解释器: $PY"

fail=0

echo
echo "== 1/6 策略高危 action 断言 =="
"$PY" ci/check-policies.py policies/cdtguard-runtime-policy.json || fail=1

echo
echo "== 2/6 基础卫生：常见凭据形态 =="
# 占位符形如 <YOUR_XXX>，不会命中下面这几条。
#
# ⚠️⚠️ **本步刻意不排除任何路径** —— 包括 ci/ 自己。
#    早期版本写的是 `| grep -v '^\./ci/' | grep -v '^\./docs/pitfalls\.md'`，
#    理由是「ci/ 里放着检测规则本身、pitfalls.md 要引用形态举例，怕自匹配」。
#    实测：纳入扫描后**一条误报都没有**（规则的写法本来就不构成凭据形态），
#    而代价是**整块盲区**。
#    这正是 ci/check-disclosure.py 里记录过的那次真实事故的成因：
#      「本文件的 docstring 里曾列出过几个已清除的值，而 SKIP_DIRS 让它永远扫不到自己。」
#    两个相邻的闸不能对同一件事持相反策略 —— 见 check-disclosure.py 的 SKIP_DIRS 注释。
if grep -rnE 'LTAI[0-9A-Za-z]{12,}|BEGIN [A-Z ]*PRIVATE KEY|(?:sk|pk|ghp|gho|glpat)[-_][A-Za-z0-9_-]{20,}' \
      --include='*.py' --include='*.js' --include='*.json' --include='*.sh' \
      --include='*.md' --include='*.yaml' --include='*.yml' . 2>/dev/null; then
  echo "  X 发现疑似凭据（见上）"
  fail=1
else
  echo "  OK 未发现凭据形态"
fi

echo
echo "== 3/6 泄漏闸：形状 + 哈希黑名单 =="
# 这一步防的是「已清除的值回流」以及「第一次出现的凭据形状」。
# ⚠️ 它只管形状与已知值 —— 「部署者特有的选择」类问题（用了哪个网络方案、哪家云、
#    伪装成哪个域名）抓不到，发布前仍须按 docs/pre-commit-checklist.md 人读。
"$PY" ci/check-disclosure.py . || fail=1

echo
echo "== 4/6 %-格式化：占位符与实参个数 =="
# 这类不匹配**只在运行时炸**，而且往往炸在**告警路径**上
# （「守护已启动」那条推送就在 main() 开头 —— 进程会启动 2 秒内死掉，
#   而 supervise-daemon 无限重启它，rc-service 仍显示 started）。
# py_compile 看不见，所以单独设一道闸。
"$PY" ci/check-format-args.py || fail=1

echo
echo "== 5/6 未定义名 / 定义了但从未被调用（Python）=="
# ⚠️ 这一步以前**只**在部署脚本里跑（deploy-cdtguard.sh 把它 scp 到目标机再跑），
#    于是仓库自身的 CI 从来不看它 —— README 却把「未定义名」列成 ci/ 的检查项之一。
#    「本地能跑」和「每次跑」是两件事，见 .github/workflows/ci.yml 顶部那段。
#
# ⚠️ 用 `while read` 而不是 `for f in $(...)`：后者会**按空白切词**，
#    文件名里有空格就会把一条路径拆成两条、两条都解析失败（而且报错很难懂）。
#    本仓库现在没有带空格的文件名，但这个循环是会被复制的模板 —— 别留这个坑。
#    `git ls-files -z` + `read -d ''` 才是 POSIX 下处理任意文件名的写法（见下）。
#
# ⚠️ 还要处理「一个 .py 都没有」：`fail` 必须保持 0，不能因为循环没进就漏判。
found=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  found=$((found + 1))
  "$PY" ci/check-names.py "$f" || fail=1
done <<EOF
$(git ls-files '*.py')
EOF
if [ "$found" -eq 0 ]; then
  echo "  没找到任何 .py —— 检查范围疑似被收窄，判为失败"
  echo "  ⚠️ 若你是**解压 zip** 而不是 git clone，本步会必然失败（zip 里没有 .git，"
  echo "     \`git ls-files\` 取不到文件列表）。这种情况下请改用 clone，"
  echo "     或手动跑：python3 ci/check-names.py <你的文件>"
  fail=1
fi

echo
echo "== 6/6 JS 语义：fc-keeper 的决策分支冒烟 =="
# 🔴 **这一步是补一个真实踩过的盲区。**
#    本仓库在 fc-keeper 上栽过两次，**两次 CI 全绿**：
#      1) 块作用域 ReferenceError（`st` 声明在 try 内、try 外引用）——
#         `node --check` **查不出**（语法合法），被外层 catch 吞掉 ⇒ 静默退化。
#      2) OSS 对象读到但没有可用 `ts` ⇒ 失联判据被算成 0 ⇒ 告警永不触发。
#    两次都只有「真的执行到那条分支」才会暴露。所以这一步**桩掉外部 IO、
#    把每个决策分支真的跑一遍并断言 action** —— 判据是 action，
#    不是「没抛异常」（bug 1 恰恰会被 catch 转换成不抛异常）。
#
#    ⚠️ 为什么单列一步而不是塞进 5/6：它需要 node，而 5/6 只需要 python。
#       缺 node 时**必须判失败**，不能静默跳过 —— 否则闸门形同不存在。
#
#    ⚠️ node 的查找要兼容 Windows 的 Git Bash / WSL：那边装的常是 `node.exe`，
#       而 `command -v node` 在 bash 里**找不到它**（实测：文件在
#       `/mnt/c/Program Files/nodejs/node.exe`，但 `command -v node` 报 not found）。
#       所以逐个试 node / node.exe，并且**要求它真的能打印版本**才算可用 ——
#       与上面找 Python 用的是同一套判据（占位符可执行文件也会「找得到」）。
NODE=""
for c in node node.exe; do
  if command -v "$c" >/dev/null 2>&1 && [ -n "$("$c" --version 2>/dev/null)" ]; then
    NODE="$c"; break
  fi
done
# Git Bash / WSL 兜底：直接试 Windows 的常见安装路径
if [ -z "$NODE" ]; then
  for p in "/mnt/c/Program Files/nodejs/node.exe" \
           "/c/Program Files/nodejs/node.exe" \
           "/c/Program Files (x86)/nodejs/node.exe"; do
    if [ -x "$p" ] && [ -n "$("$p" --version 2>/dev/null)" ]; then NODE="$p"; break; fi
  done
fi

if [ -z "$NODE" ]; then
  echo "  X 找不到可用的 node —— 本步无法执行。"
  echo "    fc-keeper 是唯一没有语义检查的代码文件，这一步是它唯一的闸。"
  echo "    装 node 后重跑（见 docs/toolchain.md 第一节）。"
  fail=1
else
  echo "  使用 node: $NODE ($("$NODE" --version 2>/dev/null))"
  "$NODE" ci/smoke-fc-keeper.js || fail=1
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "全部通过"
else
  echo "有检查未通过"
fi
exit "$fail"
