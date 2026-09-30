#!/bin/sh
# 部署 cdtguard.py 到 ECS。
#
#   用法: deploy-cdtguard.sh <本地 cdtguard.py> <ssh 目标> [--no-restart]
#   例:   deploy-cdtguard.sh ./cdtguard.py root@1.2.3.4
#
# 沿用本仓库既有的「五步范式」：
#   写临时 → 静态检查 → 带时间戳备份 → 覆盖 → 再检查 → 重启 → 验收
#
# ⚠️ 为什么每一步都要检查：这套守护的失效模式**大多是静默的** ——
#    它跑在云主机里，出问题时你不会看到报错，只会看到流量继续烧。
#
#    典型失效：`refresh_creds()` 的**定义**部署上去了、**主循环里的调用漏了**。
#    凭据 6 小时后过期，此后守护的每一次 API 调用都失败（OSS 403 / CDT 400），
#    而服务状态一直显示 `started`、日志里也没有崩溃 ——
#    **带病运行到你偶然发现为止**。
#    `py_compile` 查不出（语法没错）、导入也查不出（不执行那个函数）。
#
#    所以本脚本在部署前后各跑一次 ci/check-names.py（两个方向都查）。
#
# ⚠️ 一个真实差异：本仓库那份与**你自己那份**凭据块结构往往不同 ——
#    有人把值直接写成常量，本仓库一律用
#    `os.environ.get("ALIYUN_AK", "<YOUR_AK_ID>")` 这类回落。
#    所以部署**就是逐字覆盖**（STEP 4.6 的 cp）—— 仓库里那份带 `<YOUR_*>` 占位符，
#    覆盖之后线上就是占位符。其中：
#      · AK/SK —— 有实例 RAM 角色兜底，占位符不致命（守护优先走角色，不读文件里的值）
#      · PUSH_TOKEN —— **角色覆盖不到它**（推送网关不是阿里云服务），成了占位符
#        就 ⇒ 告警静默失效（停机仍会执行，只是你不被告知）
#      · OSS_BUCKET / BW_ID —— 同理，状态存不了、混合口径降级
#    这正是 STEP 4.5 存在的理由：**覆盖之前**先把这些查出来。
set -e

LOCAL="${1:?用法: deploy-cdtguard.sh <本地 cdtguard.py> <ssh 目标> [--no-restart]}"
TARGET="${2:?缺 ssh 目标，例如 root@1.2.3.4}"
RESTART=1
[ "$3" = "--no-restart" ] && RESTART=0

HERE=$(cd "$(dirname "$0")/.." && pwd)
CHECKER="$HERE/ci/check-names.py"
REMOTE=/root/cdtguard.py
STAMP=$(date +%Y%m%d-%H%M%S)

say() { printf '\n== %s ==\n' "$1"; }

[ -f "$CHECKER" ] || { echo "X 找不到 $CHECKER"; exit 1; }

say "STEP 0 —— 本地文件"
[ -f "$LOCAL" ] || { echo "X 找不到 $LOCAL"; exit 1; }
echo "  $LOCAL  $(wc -l < "$LOCAL") 行  sha256[:16]=$(sha256sum "$LOCAL" | cut -c1-16)"
if grep -qU "$(printf '\r')" "$LOCAL" 2>/dev/null; then
  echo "  ⚠ 含 CRLF 行尾 —— 建议转成 LF 再部署（行尾会影响 token 流与某些解释器行为）"
fi

say "STEP 1 —— 上传脚本与检查器"
scp -q "$LOCAL"   "$TARGET:$REMOTE.new"
scp -q "$CHECKER" "$TARGET:/root/check-names.py"
echo "  已上传 $REMOTE.new 与 /root/check-names.py"

# ── STEP 1.5：确保 OpenRC 服务定义存在 ──────────────────────────────
# ⚠️ 这一步不能省。本脚本后面每一处 `rc-service cdtguard …` 都依赖它；
#    没有它，`rc-service cdtguard status` 报「service not found」，
#    而 STEP 5/6 的验收会整段失败 —— 读者看到的现象是「部署完了但服务起不来」。
#    本仓库早期版本把服务定义**只以注释形式**放在 cdtguard.py 尾部，是错的。
say "STEP 1.5 —— 确保 OpenRC 服务定义存在"
INITD="$HERE/scripts/cdtguard.initd"
if [ -f "$INITD" ]; then
  scp -q "$INITD" "$TARGET:/etc/init.d/cdtguard"
  ssh "$TARGET" 'chmod +x /etc/init.d/cdtguard && rc-update add cdtguard default'
  echo "  已安装 /etc/init.d/cdtguard 并加入 default 运行级"
else
  echo "  ⚠ 找不到 $INITD —— 服务定义没装上，后面的 rc-service 会失败"
fi
# 顺带确认 python3 存在（精简镜像默认不带，见 scripts/build-image.sh 的说明）
ssh "$TARGET" 'command -v python3 >/dev/null 2>&1 && echo "  python3: $(python3 -V 2>&1)" || { echo "  X 目标机没有 python3 —— 守护跑不起来（镜像里要 apk add python3，见 scripts/build-image.sh）"; exit 1; }'

say "STEP 2 —— 部署前静态检查"
ssh "$TARGET" "cd /root && python3 check-names.py $REMOTE.new"

say "STEP 3 —— 带时间戳备份"
ssh "$TARGET" "[ -f $REMOTE ] && cp -p $REMOTE $REMOTE.bak.$STAMP && echo '  备份: $REMOTE.bak.$STAMP' || echo '  （当前无 $REMOTE，首次部署）'"

say "STEP 4 —— 部署前复检（**先不覆盖线上那份**）"
# ⚠️⚠️ 这一步**只校验 .new**，真正的覆盖推迟到 STEP 4.5 的门禁通过之后。
#    早期版本在这里直接 `cp $REMOTE.new $REMOTE` 再查占位符 —— 顺序是反的：
#    占位符没填时，线上文件**已经被换成带 `<YOUR_*>` 的版本**，而脚本随后
#    `exit 1` 且不重启。此时进程还在跑旧代码（内存里），磁盘上却是坏的；
#    下一次重启（supervise-daemon respawn / 重启主机 / 手工 restart）
#    就会带着占位符起来 —— 告警静默失效、状态写不出去，
#    而 `rc-service status` 仍然显示 started。**坏的部署 + 看起来正常**，
#    正是本脚本存在的意义所要防的那种状态。
ssh "$TARGET" "python3 -m py_compile $REMOTE.new && echo '  py_compile OK' && cd /root && python3 check-names.py $REMOTE.new"

say "STEP 4.5 —— 占位符自检（本仓库那份带 <YOUR_*>，有几处必须手改）"
# 为什么必须有这一步：本脚本是**逐字覆盖**（STEP 4.6 的 cp）。
# 仓库里那份带着 <YOUR_*> 占位符，覆盖后线上就是占位符。
#
# ⚠️⚠️ **关键前提：这个部署形态下，环境变量送不进去。**
#    `supervise-daemon` 会清空子进程环境（实测：environ 里只剩 HOME/PATH/PWD/
#    SHLVL/USER 一类的变量）。所以下面这些值**只能写进 /root/cdtguard.py 本身** ——
#    不要指望 export 或在 /etc/conf.d/cdtguard 里写。
#
# 分两档：下面这些是**致命**的，没填就 `exit 1`，不让脚本假报成功。
# ⚠️ 远端脚本必须自己把失败**带出来**。
#    早期版本在这里写了一句注释说「chk 返回非 0 时 ssh 会带出非 0」——
#    **那是错的**：远端 `sh -s` 的最后一条命令是 echo，且远端没有 set -e，
#    所以 chk 的返回值根本到不了本地。后果是：没有 RAM 角色、AK/SK 没填时，
#    脚本打印一串 [!!] 然后**正常退出 0** —— 正是本脚本声称要防的「假报成功」。
#    现在改成：远端累计 CRITICAL 并以非 0 退出，本地显式判它。
# ⚠️ 计数器必须在**远端脚本内部**初始化。远端是独立的 `sh -s` 进程，
#    看不到本地的变量 —— 早期版本把 CRITICAL=0 写在这里，
#    远端 `[ "$CRITICAL" != "0" ]` 在「一个都没失败」时拿到空串，
#    判成 true ⇒ **永远 exit 1**，连填对了也部署不了。
#    （这与它要修的那个 bug 是同一类：闸门只在一个方向上工作。）
set +e
ssh "$TARGET" 'sh -s' <<'REMOTE'
CRITICAL=0
ROLE=$(wget -qO- -T 10 http://100.100.100.200/latest/meta-data/ram/security-credentials/ 2>/dev/null \
       || curl -s --max-time 10 http://100.100.100.200/latest/meta-data/ram/security-credentials/ 2>/dev/null || true)
F=/root/cdtguard.py.new   # ← 检查**待安装**的那份，不是线上那份
chk() {  # chk <占位符> <说明> <后果>
  if grep -q "$1" "$F"; then
    echo "  [!!]  $1 未填 —— $2"
    echo "        后果：$3"
    echo "        改法：编辑 $F 换掉它，然后 rc-service cdtguard restart"
    CRITICAL=$((CRITICAL + 1))
    return 1
  fi
  echo "  [OK]  $2"
  return 0
}
if [ -n "$ROLE" ]; then
  echo "  [OK]  实例 RAM 角色: $ROLE —— AK/SK 走它，不需要写进文件"
  AKOK=0
else
  echo "  [!!]  取不到实例 RAM 角色 —— AK/SK 必须手写进文件，否则每次云 API 调用都失败"
  AKOK=1
  [ -z "$ROLE" ] && { echo "        （角色为空 ⇒ 若下面 AK/SK 也是占位符，本次部署必失败）"; }
fi
[ "$AKOK" = "1" ] && chk '<YOUR_AK_ID>' 'ALIYUN_AK' '守护所有云 API 调用失败'
[ "$AKOK" = "1" ] && chk '<YOUR_AK_SECRET>' 'ALIYUN_SK' '同上'
chk '<YOUR_PUSHPLUS_TOKEN>' 'PUSH_TOKEN' '告警发不出去（停机仍执行，只是你不被告知）'
chk '<YOUR_OSS_BUCKET>' 'OSS_BUCKET' '状态存不了 —— 守护的跨实例重建接续、FC 的拉起熔断与「护闸失联」告警全部静默失效'
chk '<YOUR_CBWP_ID>' 'BW_ID（共享带宽包实例 ID）' '云监控查不到数据 ⇒ 混合口径静默退化为纯网卡口径（安全但精度降级）'
echo
echo "  --- 下面这几项会**自动纠正**，留占位符不会致命 ---"
# ⚠️ 必须按**赋值行**判（^VAR=），不能全文件 grep：这些占位符在注释里也出现，
#    一旦注释里有，全文件 grep 会**永远**报「未填」，即使你已经填好了。
chk_opt() {  # chk_opt <变量名> <说明>
  # ⚠️ 必须同时认**两种取值形态**，否则会对着未填的占位符报「已填」：
  #      X = "<YOUR_...>"
  #      X = os.environ.get("X", "<YOUR_...>")
  #    只写 `^VAR *= *["']<YOUR_` 会漏掉第二种 —— 而仓库里用的正是第二种。
  if grep -qE "^$1 *= *(os\.environ\.get\([^,]+,[[:space:]]*)?[\"']<YOUR_" "$F"; then
    echo "  [--]  $1 仍是占位符（$2）"
  else
    echo "  [OK]  $1 已填"
  fi
}
chk_opt INSTANCE_ID "守护会从元数据反查自纠"
chk_opt EIP         "守护会从元数据反查自纠"
chk_opt INST_NAME   "⚠️ **不会自纠** —— 留占位符则所有告警里的实例名都是占位符"
echo
if [ "$CRITICAL" != "0" ]; then
  echo "CRITICAL_FAIL=$CRITICAL"
  exit 1
fi
echo
echo "  --- 阈值 ---"
grep -nE '^THRESHOLD_ENV|^BOOT_REGION' "$F" | sed 's/^/  /'
echo "  阈值按实测地域自动取（内地 20 / 非内地 200）。想留余量必须**手改文件**："
echo "  把 THRESHOLD_ENV 那一行改成 THRESHOLD_ENV = \"185\"（环境变量送不进来）。"
REMOTE
#
# ⚠️⚠️ **AK/SK 只在「没有实例 RAM 角色」时才要求手填。**
#     它们是**推荐形态下故意保留的回落值** —— 有实例 RAM 角色时，守护走角色拿凭据，
#     根本不读文件里那两个值（见 cdtguard.py 头部的凭据说明）。
#     早期版本把它们也放进这个 grep，结果是：**照着文档走（用 RAM 角色）永远部署不成功，
#     而唯一能通过的写法恰好是仓库明令禁止的「在实例上放长期 AccessKey」。**
#     那两项目标由上面的 chk 按「有没有 RAM 角色」条件判断，不在这里一刀切。
GATE=$?
set -e
if [ "$GATE" -ne 0 ]; then
  echo
  echo "X 上面标 [!!] 的占位符还没填 —— 部署不算完成。"
  echo "  它们只能写进那份**待安装的**文件（supervise-daemon 会清空环境变量）。"
  echo "  两条收敛路径，选一条："
  echo "    a) 改**本地**那份再跑本脚本（推荐，可重复）"
  echo "    b) 直接 ssh 上去改进线上的 $REMOTE 再 rc-service cdtguard restart"
  echo "       ⚠️ 走 b) 就**别再跑本脚本** —— 它会用本地那份重新覆盖，"
  echo "          把你手工改的抹掉，然后同一个门禁又会失败。"
  echo "  注：本步失败时**线上文件没有被改动**（覆盖已推迟到门禁之后）。"
  exit 1
fi

say "STEP 4.6 —— 门禁通过，覆盖线上文件"
# ⚠️ 覆盖放在最后：只有确认占位符都填了，才把线上那份换掉。
#    顺序反过来（先覆盖再检查）会让「没填」的结局变成「坏文件已上盘」。
# ⚠️ `.prev` 是就地的上一版（STEP 3 那份带时间戳的是长期备份）。
#    首次部署时 $REMOTE 还不存在 —— 用 `|| true` 放行，别让 set -e 把它当失败。
ssh "$TARGET" "cp -p $REMOTE $REMOTE.prev 2>/dev/null || true; cp $REMOTE.new $REMOTE && python3 -m py_compile $REMOTE && echo '  已覆盖并复检 OK'"

if [ "$RESTART" -eq 0 ]; then
  say "STEP 5 —— 已指定 --no-restart，到此为止"
  echo "  手动重启: ssh $TARGET 'rc-service cdtguard restart'"
  exit 0
fi

say "STEP 5 —— 重启"
ssh "$TARGET" "rc-service cdtguard restart"
sleep 8

say "STEP 6 —— 验收"
cat <<'NOTE'
  ⚠️ 下面两个指标**不要**用来验收 —— 它们能通过一个已经死掉的守护：
       · 「:443 在监听」  那测的是另一个进程（sing-box），恒真
       · 「日志里 ERR=0」 死掉的守护不写日志，恒为 0
     真正该看的是：进程在、**状态文件的 mtime 在动**（约每 2 秒一次）、关键日志行都有值。
NOTE
echo
ssh "$TARGET" '
  echo "  [1] 服务状态: $(rc-service cdtguard status)"
  echo "  [2] 进程数:   $(ps aux | grep "[c]dtguard.py" | wc -l)"
  echo "  [3] 状态文件（看 mtime，秒级）:"
  for f in /var/lib/cdtguard/state.json /root/.cdtguard.state; do
    [ -f "$f" ] && echo "      $f  $(date -r "$f" "+%H:%M:%S" 2>/dev/null || stat -c %y "$f")"
  done
  [ -f /var/lib/cdtguard/state.json ] || echo "      找不到（首次部署正常）"
  echo "  [4] 日志最后 8 行:"
  tail -8 /var/log/cdtguard.log | sed "s/^/      /"
'
echo
echo "  [5] 等十几秒后**再看一次** [3] —— mtime 应当变了（写盘约每 2 秒一次）"
echo "      ssh $TARGET 'date -r /var/lib/cdtguard/state.json'" 
echo
echo "  回滚: ssh $TARGET 'cp $REMOTE.bak.$STAMP $REMOTE && rc-service cdtguard restart'"
