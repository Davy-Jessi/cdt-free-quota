#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公开前的泄漏闸：形状检查 + 哈希黑名单。

用法：python3 ci/check-disclosure.py [目录]        默认 .
     python3 ci/check-disclosure.py --list-shapes  只看形状规则

═══ 为什么需要它 ═══
本仓库的历史上反复出现同一类事故：**每报一次「脱敏完成」，换一个语义类别就又能找出新泄漏。**
根因是当时只用「模式清单」逐轮扫描 —— 而清单只能找到**已经想到的**类别。
所以这道闸不是「更全的模式清单」，它是两件事：

  ① **形状检查** —— 不看具体值，只看**形态**。凭据、账号 ID、资源 ID、公网 IP
     各有固定形状，与它在讲什么无关。这一层能抓到**第一次出现**的泄漏。
  ② **哈希黑名单** —— 把本仓库历史上清除过的具体值以 SHA256 形式记住，
     防止它们（或它们的变体）在后续编辑里**回流**。这一层是回归护栏。

═══ ⚠️ 设计约束：检查器自己不能是泄漏源 ═══
第 ② 层**只存哈希、不存原文**，命中时也只报「第 N 条黑名单项」而不打印命中值 ——
否则这个脚本躺在公开仓库里，本身就成了敏感值的清单。

═══ ⚠️ 它抓不到的（必须靠人读）═══
「**这是部署者特有的选择**」这类问题 —— 例如用了哪种网络方案、哪家云、挑了哪个域名、
给哪些服务分流。这些**没有固定形状**，也没有已知值可以黑名单。
所以本脚本通过 **≠** 可以发布。发布前仍必须按
`docs/pre-commit-checklist.md` 的语义清单**逐条人读一遍**。
"""
import hashlib
import os
import re
import sys

# ── 第 ② 层：哈希黑名单（**不在仓库里**）─────────────────────────────
#
# 🔴🔴 **为什么这里没有一份硬编码的名单。**
#
# 把「已清除的值」以 SHA256 形式**提交进仓库**是**错的**，两个原因：
#
#   1. **对低熵值，哈希不是承诺。** 一条 IPv4 只有 2³² 种可能 ——
#      单核 Python 一小时、GPU 几秒就能穷举还原。8 位十六进制同理。
#      「只存哈希不存原文」这句话对高熵凭据成立，**对这类值不成立**。
#   2. **分类注释本身就是字典。** 给每条标上「这是哪一类」，
#      等于把搜索空间从「任意字符串」缩到某个很窄的形态集合 ——
#      拿到的是**带了提示的**目标集。
#
# 所以：**名单不进仓库。** 由使用者放在仓库外，脚本存在时自动加载。
#
# 本地名单格式（每行一条 SHA256，`#` 开头为注释）：
#     ci/.denylist
#
# ⚠️ `ci/.denylist` 已写进 `.gitignore`。**别改回硬编码。**
def _load_denylist(root):
    """从仓库外的 ci/.denylist 加载黑名单。

    ⚠️⚠️ **没有这个文件时，第 ② 层是空转的 —— 必须把这件事说出来，不能默默 OK。**
       早期版本直接 `return set()`，于是脚本照样打印「OK no shape or denylist hits」，
       而 README 却把这条描述成「防已清除值回流」的回归护栏。
       **「这层没在跑」和「这层跑了没发现问题」打印出来一模一样** ——
       正是本仓库反复批评的那类假信号。所以现在返回 (集合, 是否存在) 二元组。

    🔴🔴 **写名单的人注意：名单里的哈希必须是「切分后的 token」的哈希，不是原文的。**
       扫描时走的是 `candidates()`：它按 `[A-Za-z0-9][A-Za-z0-9._:-]{2,}` 切词，
       再对每个 token 取长度 ≥ 8 的前缀。所以：

         · 值里含 `=`、空格、`/` 等不在字符集里的字符 ⇒ 原文会被切成好几段，
           **对整串做 sha256 永远对不上**
         · 值以 `_` 或 `-` 开头 ⇒ 字符集要求首字符是字母数字，**前导符号被吃掉**

       ⚠️ **本函数刻意不举任何一个真实值的例子。** 本仓库在这上面栽过两次：
         第一次是把已清除的值写进本文件的 docstring，而当时的 SKIP_DIRS
         让本文件扫不到自己；第二次是给黑名单写使用说明时，
         又把「什么样的值会哈希失败」举成了真实值 —— 于是那几行本身
         就构成了泄漏，而且正好被新加的黑名单抓个正着（这次是闸自己发现的）。
         **说明文字只描述形状，不点名。**

       **结论：「我把它加进黑名单了」不等于「它会被抓到」。**
       加完必须做一次正向测试 —— 把那个值写进一个临时文件、跑本脚本、
       确认真的出现 `[denylist]` 命中。**没做过正向测试的名单等于没加。**

       🔴 **生成名单时只取 `candidates()` 里的「完整 token」，不要用它的前缀展开。**
          `candidates()` 会把每个 token 的**所有 ≥ 8 字符前缀**也放进结果集，
          那是为了在**扫描**时抓到被省略号截断的值；但**生成名单**时沿用它，
          就会把 `xxx.py.new` 里的 `xxx`、`xxx.py` 一并注册成黑名单项 ——
          而这些前缀在正常散文里到处都是。实测这么做会在一个干净仓库上
          产生近百处误报。**闸门一旦有噪声就会被放宽，那等于关掉它。**
          正确做法：只哈希完整 token，然后逐条做正向测试。
    """
    path = os.path.join(root, "ci", ".denylist")
    try:
        out = set()
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#"):
                out.add(line)
        return out, True
    except OSError:
        return set(), False

# ── 第 ① 层：形状规则 ────────────────────────────────────────────────
# 每条 = (名字, 正则, 说明)。正则针对**已剥离占位符**的文本。
SHAPES = [
    ("阿里云 AK", r"(?<![A-Za-z0-9])LTAI[0-9A-Za-z]{12,}",
     "AK 形如 LTAI + 16~20 位。仓库里**任何**真实 AK 都是事故。"),
    ("裸 16 位账号 UID", r"(?<![0-9])[0-9]{16}(?![0-9])",
     "阿里云账号 UID 是 16 位数字。桶名里也常嵌它。"),
    ("UUID", r"(?<![0-9a-fA-F])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
     "VLESS/VMess 的 UUID 是**凭据**。示例里必须用 <YOUR_UUID>。"),
    ("资源 ID", r"(?<![A-Za-z0-9])(?:i|cbwp|eip|vpc|vsw|sg|rg|eni|disk)-[a-z0-9]{8,}",
     "真实的实例/带宽包/EIP/网络资源 ID。占位符形如 <eip-xxx>，长度不足不会命中。"),
    ("32 位十六进制", r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])",
     "pushplus token 等 32 位 hex 凭据的形态。**大小写都要抓** —— "
     "早期版本写的是 [0-9a-f]（只认小写），实测 0123…CDEF 会漏。"),
    ("64 位十六进制", r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])",
     "SHA256 形态。**这是一个真实的漏检类别** —— 曾经有一份变更存档里留着"
     "线上部署包的完整 sha256（那等于公开一个正在运行的产物的指纹），"
     "而当时的规则集里没有这一条，所以它一直躺着没被拦。"),
    ("公网 IPv4", r"(?<![0-9.])(?:[1-9][0-9]?|1[0-9]{2}|2[0-4][0-9]|25[0-5])(?:\.(?:[0-9]{1,3})){3}(?![0-9])",
     "需要人工确认是否为真实公网 IP（白名单见下）。"),
]

# 公网 IP 的白名单：官方文档地址、保留段、示例段 —— 这些**可以**出现
#
# ⚠️⚠️ **白名单是闸门上的洞，每加一条都要能说出「为什么它一定不是真实值」。**
#    早期版本这里有一条「阿里云某官方示例段」的 /16 白名单 ——
#    但那其实是**阿里云真实的公网段**，一旦仓库里出现该段的真实 IP 就会被静默豁免。
#    **真实云厂商网段不能进白名单。** 已删除，且下面不再用字面量复述它
#    （复述本身就会命中 IP 形状规则 —— 这就是本文件第 ③ 条要防的同一类事故）。
IP_ALLOW = [
    re.compile(r"^127\."),                    # 环回
    re.compile(r"^0\."),                      # 未指定
    re.compile(r"^255\."),                    # 广播
    re.compile(r"^100\.100\."),               # 阿里云元数据/内网 DNS（官方公开地址）
    re.compile(r"^10\."), re.compile(r"^192\.168\."),
    re.compile(r"^172\.(1[6-9]|2[0-9]|3[01])\."),
    re.compile(r"^169\.254\."),               # 链路本地
    re.compile(r"^198\.51\.100\."), re.compile(r"^203\.0\.113\."),  # RFC 5737 文档示例
    re.compile(r"^1\.2\.3\.4$"),              # 惯例上的「随便一个 IP」，非真实地址
]

# 这些路径/后缀不参与检查。
#
# ⚠️⚠️ **只跳过 .git 与缓存目录 —— 不要把 ci/ 或 .github/ 也跳掉。**
#     早期版本写的是 {".git", "ci", ".github"}，理由是「ci/ 里放着检测规则本身，
#     怕自匹配」。实测：把两者纳入扫描后**一条误报都没有**，
#     而代价是**整块盲区** —— 任何落在 ci/ 或 .github/ 里的泄漏永远不会被抓到。
#     这正是一次真实事故的成因：本文件的 docstring 里曾列出过几个已清除的值，
#     而 SKIP_DIRS 让它**永远扫不到自己**。
#     「怕误报」不能靠扩大白名单解决 —— 那等于把闸门关掉。
#     （下面的覆盖自检会把这条约束变成断言，见 main() 末尾。）
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".playwright-mcp"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".gz", ".pdf"}
# ⚠️ 黑名单文件本身要跳过：它是「一串 SHA256」，形状规则会把它自己抓出来
#    （每条都是一段 64 位 hex）。它的价值在于内容比对，不在于被扫。
#    注意这里跳的是**单个文件名**，不是 ci/ 整个目录 —— ci/ 仍必须被扫。
SKIP_FILES = {"ci/.denylist"}


def candidates(text):
    """把文本切成候选 token —— 用于第 ② 层的哈希比对。

    ⚠️ **本函数（以及本文件）刻意不举任何真实例子** —— 本仓库踩过这个坑：
    为了解释「删掉了什么」，就在注释里把那个值又写了一遍。所以这里只描述
    **形状**，不点名：切分字符集包含点、下划线、冒号、连字符，
    这样「域名 / 带连字符的标识符 / 带点分隔的字符串」会各自成为一个整 token；
    另外对每个 token 再取长度 ≥ 8 的前缀，以便抓到被省略号截断的值。"""
    out = set()
    for m in re.finditer(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,}", text):
        tok = m.group(0)
        out.add(tok)
        for n in range(8, min(len(tok), 40) + 1):
            out.add(tok[:n])
    return out


def strip_placeholders(text):
    """把 <YOUR_...> / <你的...> 占位符挖空，避免形状规则误报占位符本身。

    ⚠️⚠️ **这个函数同时也是一个逃逸通道，必须知道它的边界。**
    「把尖括号里的内容整个删掉」意味着：任何真实值只要被人为套上一对尖括号，
    就对第 ① 层的形状检查**隐形**了。例如把一个真实账号标识写成
    尖括号包起来的纯数字串。

    为什么不能简单「不删」：占位符 `<YOUR_OSS_BUCKET>` 之类本身要能过闸，
    而散文里也大量出现尖括号包裹的说明文字（`<上一步的 url>`）。

    所以这里采取**折中**：只挖空**看起来像占位符的**尖括号内容 ——
    即内部**含字母、下划线或中文**的（`<YOUR_X>` / `<eip-xxx>` / `<你的域名>`）。
    **纯数字或纯 hex 的尖括号内容一律保留**，让它照常被形状规则抓到。
    这样占位符被挖空，而尖括号包起来的真实标识会被第 ① 层抓住。
    """
    def keep_if_suspicious(m):
        inner = m.group(1)
        # 纯数字 / 纯 hex ⇒ 这是**真实值**的形态（账号 UID、token、哈希），保留待检。
        # 其余（含字母、下划线、中文）当占位符处理 —— `<YOUR_*>` / `<eip-xxx>` / `<你的域名>`。
        if re.fullmatch(r"[0-9A-Fa-f]+", inner):
            return m.group(0)
        return "<YOUR_PLACEHOLDER>"

    return re.sub(r"<([^<>\n]{1,60})>", keep_if_suspicious, text)


def main():
    # ⚠️ 让输出永不因编码而崩：某些控制台的默认编码不是 UTF-8，
    #    print 非 ASCII 会抛 UnicodeEncodeError 并让 CI 误报失败（实测踩过）。
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    if "--list-shapes" in sys.argv:
        for name, rx, why in SHAPES:
            print("%-16s %s\n%s" % (name, rx, why))
        return 0

    banned, has_denylist = _load_denylist(root)
    bad = 0
    checked = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in SKIP_EXT:
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, root)
            # ⚠️ 归一化分隔符再比对 —— Windows 上 relpath 给的是 `ci\.denylist`
            rel_key = rel.replace(os.sep, "/")
            if rel_key in SKIP_FILES:
                continue
            try:
                text = open(path, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue
            checked += 1
            stripped = strip_placeholders(text)

            # ① 形状
            for name, rx, why in SHAPES:
                for m in re.finditer(rx, stripped):
                    val = m.group(0)
                    if name == "公网 IPv4" and any(a.match(val) for a in IP_ALLOW):
                        continue
                    line = stripped[:m.start()].count("\n") + 1
                    print("X %s:%d  [shape] %s" % (rel, line, name))
                    print("    %s" % why)
                    bad += 1

            # ② 哈希黑名单（只报序号，不报原文）
            for tok in candidates(stripped):
                h = hashlib.sha256(tok.encode()).hexdigest()
                if h in banned:
                    print("X %s  [denylist] 命中本地黑名单（原文不打印）" % rel)
                    bad += 1

    print()
    # ── ③ 脱敏标记落进了**代码位置** ────────────────────────────────
    # 🔴 这是一次真实事故，值得单独设一道闸：
    #    给日期做脱敏时，正则把云 API 的 **Version 参数**也吃了 ——
    #      rpc("DescribeInstances", "2014-05-26", ...)
    #      → rpc("DescribeInstances", "<日期已隐去>", ...)
    #    语法没坏、py_compile 通过、看起来也「像占位符」，
    #    但**每一次云 API 调用都会失败**。而它躲过了当时所有检查：
    #    形状检查不认它、人读时看到「已隐去」还以为是有意为之。
    #
    # 判据：**字符串字面量里的占位符只允许一种合法形态 —— `<YOUR_*>`。**
    #       其他任何 `<...>` 出现在代码的字符串字面量里，一律当事故报出来。
    #       （注释与 docstring 里的不算 —— 那里讲的就是「用 <YOUR_XXX> 替换」。）
    STR_LIT = re.compile(r"""(['"])((?:[^'"\\]|\\.)*?)\1""")
    PLACEHOLDER_OK = re.compile(r'^<YOUR_[A-Z0-9_]+>$')
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in (".py", ".js", ".sh", ".json", ".yml", ".yaml"):
                continue
            fp = os.path.join(dp, fn)
            rel2 = os.path.relpath(fp, root)
            try:
                raw = open(fp, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue
            in_doc = False
            for ln, line in enumerate(raw.split("\n"), 1):
                st = line.strip()
                # JSON/YAML 没有注释语法，整份跳过（它们本来就只有数据）
                if ext in (".json", ".yml", ".yaml"):
                    continue
                # Python 三引号块（docstring）跟踪
                if ext == ".py" and st.count('"""') % 2 == 1:
                    in_doc = not in_doc
                    continue
                if in_doc:
                    continue
                # 行注释
                if st.startswith("#") or st.startswith("//") or st.startswith("*"):
                    continue
                code = line.split(" #")[0] if ext == ".py" else line.split("//")[0]
                for m in STR_LIT.finditer(code):
                    lit = m.group(2)
                    # ⚠️ 只认「**整个字面量就是一个占位符**」这一种形态。
                    #    不去匹配「散文里嵌了尖括号」的字符串 —— 那些是给人看的
                    #    说明文字（本文件自己的 SHAPES 消息就是），误报会把闸门
                    #    淹掉，而闸门一旦有噪声就会被放宽（那就等于关掉它）。
                    #    真实事故的形态恰恰是**整串就是占位符**：
                    #      rpc("DescribeInstances", "<日期已隐去>", ...)
                    if not re.fullmatch(r"<[^<>]+>", lit):
                        continue
                    if PLACEHOLDER_OK.match(lit):
                        continue
                    print("X %s:%d  [code-placeholder] 字符串字面量里有个非 <YOUR_*> 的占位符"
                          % (rel2, ln))
                    print("    字面量：%r" % lit[:80])
                    print("    占位符只有一种合法形态 <YOUR_XXX>；代码位置出现别的尖括号")
                    print("    多半是脱敏时误伤了真实参数（版本号 / 地域 / 端点 / 日期）。")
                    bad += 1

    # ── 覆盖自检 ──────────────────────────────────────────────────
    # 「扫了多少」比「扫出什么」更容易悄悄退化：有人为了消一条误报，
    # 往 SKIP_DIRS 或 SKIP_EXT 里加一项，闸门就永久性地少看一块，
    # **而且不会有任何信号**（仍然打印 OK）。所以这里把覆盖量变成断言。
    scanned = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in SKIP_EXT:
                continue
            scanned.add(os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/"))

    # ① 本文件自己必须在被扫范围内 —— 历史上它曾因 SKIP_DIRS 含 "ci" 而扫不到自己
    if "ci/check-disclosure.py" not in scanned:
        print("X [coverage] ci/check-disclosure.py 不在扫描范围内 —— SKIP_DIRS 被放宽了")
        bad += 1
    # ② 覆盖量下限。新增/删除文件时按实际调整，但**不要为了让它过而调低**
    if checked < 30:
        print("X [coverage] 只扫到 %d 个文件（下限 30）—— 扫描范围疑似被收窄" % checked)
        bad += 1

    print("scanned %d files." % checked)
    if bad:
        print("!! %d finding(s) -- DO NOT PUBLISH." % bad)
        print("   注意：新模式加进 SHAPES；新清除的值加进 ci/.denylist（**不要提交**）。")
        return 1
    print("OK  no shape or denylist hits.")
    # ── 第 ② 层的状态必须显式打印 ────────────────────────────────
    # 「没在跑」与「跑了没问题」的输出必须能区分 —— 否则这条闸会给出假信心。
    if not has_denylist:
        print("⚠️ **第 ② 层（哈希黑名单）本次未生效** —— 找不到 ci/.denylist。")
        print("   上面那句 OK 只代表**形状检查**通过，不代表「已清除的值没有回流」。")
        print("   要启用它：把你清除过的值逐个 sha256 后写进 ci/.denylist（该文件已 gitignore）。")
    else:
        print("✅ 第 ② 层已生效：本地黑名单 %d 条。" % len(banned))
    print("⚠️ 但这**不等于可以发布** —— 「部署者特有的选择」类问题抓不到，")
    print("   仍需按 docs/pre-commit-checklist.md 的语义清单人读一遍。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
