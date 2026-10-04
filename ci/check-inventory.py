#!/usr/bin/env python3
"""check-inventory.py —— 让「文档声称的部署事实」变成一条可执行的自洽检查。

**为什么需要它（真实事故，不是假想）。**

本仓库的文档在 `scripts/provision/README.md` §5.3 里明确列了 **3 个** FC 触发器，
还专门用一段警告说 `guard-liveness-8min`「**别漏掉** —— 它是**唯一**能在护闸
悄悄死掉时发出告警的机制」。

但线上**只有 2 个**。那个触发器从来没被创建过。

也就是说：**警告写得很到位，触发器还是没建。** 因为「文档里写了」和「线上有了」
之间没有任何一步会去比对 —— 而本仓库对**别的**失效模式都建了闸：
策略有 `check-policies.py`、脱敏有 `check-disclosure.py`、JS 语义有
`smoke-fc-keeper.js`。唯独「部署事实」这一格是空的。

**这个脚本补的就是那一格**，判据是三方自洽：

    inventory.json  ←→  文档（provision §5.3 的权威表 + 各处的「N 个触发器」）
    inventory.json  ←→  源码（index.js 的 VER / env / 触发器契约）
    线上实际状态    ←→  inventory.json   ← **这一步需要凭据，不在这里**
                                            见 scripts/verify-deploy.py

⚠️ **本脚本不假装能证明线上状态。** CI 里没有云凭据，`verify-deploy.py` 跑不了。
   这里只保证「清单 ↔ 文档 ↔ 源码」三者不自相矛盾 —— 那是**必要条件**，
   不是充分条件。真正的回读是 `scripts/verify-deploy.py`，部署时自动跑。
   把这一步说成「已验证线上」是**错的**，而且正是当初漏掉它的原因。
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(".")
INV = "inventory.json"


def load_inventory():
    p = ROOT / INV
    if not p.exists():
        print("  X 找不到 %s —— 无法核对部署事实" % INV)
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print("  X %s 读不了或不是合法 JSON: %s" % (INV, e))
        return None


def check_internal(doc):
    """清单自身的一致性。键名与契约，不含任何值。"""
    bad = 0
    fc = doc.get("fc") or {}
    triggers = fc.get("triggers") or []
    envs = fc.get("requiredEnv") or []
    not_in_src = fc.get("envNotInSource") or []

    if not triggers:
        print("  X fc.triggers 为空 —— 清单没有内容可核对")
        bad += 1
    if not envs:
        print("  X fc.requiredEnv 为空")
        bad += 1

    # 触发器必须有名字/类型/作用；timer 必须有 cron，且用 6 段（FC 的 timer 含秒）
    for t in triggers:
        n = t.get("name")
        if not n:
            print("  X 有个触发器没有 name")
            bad += 1
            continue
        if t.get("type") == "timer" and not t.get("cron"):
            print("  X %s 是 timer 但没有 cron" % n)
            bad += 1
        c = t.get("cron")
        if c and len(c.split()) != 6:
            print("  X %s 的 cron 不是 6 段（FC timer 含秒）: %r" % (n, c))
            bad += 1
        if not t.get("role"):
            print("  X %s 没有 role（说不清它为什么必须存在，就无法判断能不能删）" % n)
            bad += 1
        # ⚠️ enabled 必须**显式声明**，不能靠默认值。
        #    「存在但被停用」与「不存在」是同一种失效：列表里看得见、
        #    看起来一切正常，而它一次都不会触发。
        #    所以这里要求它是一个显式的布尔，缺了就是清单没写清楚。
        if not isinstance(t.get("enabled"), bool):
            print("  X %s 没有显式的 enabled 布尔 ——"
                  "「存在但被停用」和「不存在」是同一种失效，必须声明" % n)
            bad += 1

    dup = sorted({t["name"] for t in triggers if t.get("name")
                  and [x.get("name") for x in triggers].count(t["name"]) > 1})
    if dup:
        print("  X 触发器重名: %s" % dup)
        bad += 1

    # 「存活巡检」必须恰好一个 —— 0 个 = 失去 dead-man's switch；
    # 多个 = 每 8 分钟推多条，是刷屏。
    lv = [t["name"] for t in triggers if t.get("payloadLiveness")]
    if len(lv) != 1:
        print("  X payloadLiveness 为真的触发器应恰好 1 个，实际 %d 个: %s" % (len(lv), lv))
        print("    （0 个 = 没有 dead-man's switch；多个 = 每 8 分钟刷屏）")
        bad += 1

    # envNotInSource 必须是 requiredEnv 的子集 —— 否则它是在为一个**不存在**的键辩护
    stray = sorted(set(not_in_src) - set(envs))
    if stray:
        print("  X envNotInSource 里有键不在 requiredEnv 里: %s" % stray)
        bad += 1

    if not bad:
        print("  OK 清单自洽：%d 个触发器（其中存活巡检 %d 个）、%d 个必需 env 键"
              % (len(triggers), len(lv), len(envs)))
    return bad


def check_source(doc):
    """清单 ↔ 源码。防「清单改了、代码没跟上」以及反向的漂移。"""
    bad = 0
    fc = doc.get("fc") or {}
    triggers = fc.get("triggers") or []
    envs = fc.get("requiredEnv") or []
    not_in_src = set(fc.get("envNotInSource") or [])

    # ① VER 源文件必须存在且定义了 VER（deploy-fc.py 靠它做部署后校验）
    vs = fc.get("verSource")
    vp = ROOT / vs if vs else None
    if not vs or not vp.exists():
        print("  X verSource 指向的文件不存在: %r" % vs)
        bad += 1
        src = ""
    else:
        src = vp.read_text(encoding="utf-8")
        m = re.search(r"const VER = '([^']+)'", src)
        if not m:
            print("  X %s 里找不到 `const VER = '...'` —— 无法做部署后校验" % vs)
            bad += 1
        else:
            print("  OK %s 的 VER = %s" % (vs, m.group(1)))

    # ② 每个必需 env 键都要能在源码里找到读取点（除非显式声明为平台变量）
    if src:
        missing = []
        for k in envs:
            if k in not_in_src:
                continue
            if ("process.env.%s" % k) not in src and ("requireEnv('%s')" % k) not in src:
                missing.append(k)
        if missing:
            print("  X 这些键在 %s 里既没有 process.env 也没有 requireEnv: %s"
                  % (vs, missing))
            print("    若它是平台/运行时自己读的（如 TZ），加进 fc.envNotInSource 显式声明，")
            print("    而不是让它悄悄漏过 —— 那个例外必须写在清单里。")
            bad += 1
        else:
            print("  OK %d 个必需 env 键都在源码里有读取点（%d 个已声明为平台变量）"
                  % (len(envs) - len(not_in_src), len(not_in_src)))

        # ③ 存活巡检的 payload 判据必须仍在：这是被修过一次的坑
        if "JSON.parse(ev.payload)" not in src:
            print("  X 源码里找不到 `JSON.parse(ev.payload)` ——")
            print("     存活巡检的判据依赖它。写成 !!ev.liveness 是**错的**：")
            print("     FC 把 payload 作为字符串放在 ev.payload 里。")
            bad += 1
        else:
            print("  OK 存活巡检判据仍在（先 JSON.parse(ev.payload) 再取 liveness）")

        # ④ 每个触发器名字都要在源码头部的契约注释里出现
        for t in triggers:
            n = t.get("name")
            if n and n not in src:
                print("  X 触发器 %s 没有出现在 %s 的契约注释里" % (n, vs))
                bad += 1

    return bad


def _read_text(p):
    p = ROOT / p
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


# 让一句计数变成「历史/对比陈述」的词。
#
# ⚠️⚠️ **为什么需要这张表。** 第一版规则是「任何 `N 个触发器` 都要等于清单长度」，
#    实测立刻误报两处，而**两处都是对的**：
#      · provision:564 「本仓库早期版本的依赖表只列了 2 个触发器」
#      · README:144     「但线上实际只有 2 个触发器」
#    这两句是有价值的事故记录。判红它们等于**逼人删掉准确的历史** ——
#    那比不查更糟。**闸门不能惩罚准确的历史。**
HISTORY_MARKERS = (
    "早期", "曾经", "以前", "原本", "当初", "历史", "旧版",
    "只列了", "实际只有", "实际是", "过去", "原来",
)


def _count_claims(text):
    """抽出所有「N 个触发器」形态的计数，连同位置。"""
    out = []
    for pat, kind in ((r"触发器\s*[×x]\s*(\d+)", "触发器 ×N"),
                      (r"一共\s*(?:要\s*)?(\d+)\s*个触发器", "一共 N 个触发器"),
                      (r"(\d+)\s*个触发器", "N 个触发器")):
        for m in re.finditer(pat, text):
            out.append((int(m.group(1)), kind, m.start()))
    return out


def _wrong_claims(text, want):
    """返回与 `want` 不符、且**不是**历史/对比陈述的计数。

    ⚠️ 历史标记要**贴着数字所在那一句**判断，不能整段扫 ——
       否则同一段里既讲历史又讲现状时会被整体放过。
    """
    wrong = []
    for num, kind, pos in _count_claims(text):
        if num == want:
            continue
        lo = max(text.rfind("\n", 0, pos), text.rfind("。", 0, pos)) + 1
        hi = text.find("\n", pos)
        if hi < 0:
            hi = len(text)
        sent = text[lo:hi]
        if any(w in sent for w in HISTORY_MARKERS):
            continue
        wrong.append((text[:pos].count("\n") + 1, kind, num, sent.strip()[:60]))
    return wrong


def _table_trigger_names(block):
    """从 §5.3 的 markdown 表格里抽出「第一列」的触发器名字。

    ⚠️⚠️ **不要用「段落里所有反引号词」来近似。** 第一版就是这么写的：
        `listed = set(re.findall(r"`([A-Za-z0-9_-]+)`", block))` 然后再
        `listed & names` —— 那个**交集**让「表里多出一个触发器」永远无法失败。
        反向验证时实测到了：从清单里删掉 guard-liveness-8min 后，
        脚本照样打印「OK 权威表列全了 2 个触发器」。**假通过。**
        而「表里多、清单少」恰恰就是当初那次事故的方向（文档 3 个、线上 2 个）。
        所以这里改成**精确解析表格第一列**，并且要求集合**相等**而不是包含。

    表格行的形态：`| `name` | type | cron | 作用 |`
    存活巡检那行带粗体：`| **`guard-liveness-8min`** | timer | ... |`
    """
    out = set()
    for line in block.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if not cells:
            continue
        first = cells[0]
        # 跳过表头与分隔行（---|---）
        if set(first) <= set("-: ") or not first:
            continue
        name = first.strip("*` ").strip()
        # 只认「像触发器名」的：小写字母/数字/连字符，且含连字符
        if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", name):
            out.add(name)
    return out


def check_docs(doc):
    """清单 ↔ 文档。§5.3 的权威表，以及各处的「N 个触发器」。"""
    bad = 0
    fc = doc.get("fc") or {}
    triggers = fc.get("triggers") or []
    names = {t["name"] for t in triggers}
    want = len(triggers)

    # ① 权威表：§5.3 段落里的表格必须列出**同一组**触发器名字（集合相等）
    tbl_path = doc.get("docTriggerTable")
    sect = doc.get("docTriggerTableSection") or ""
    text = _read_text(tbl_path) if tbl_path else None
    if text is None:
        print("  X 读不到权威表所在的文件: %r" % tbl_path)
        return bad + 1

    i = text.find(sect)
    if i < 0:
        print("  X %s 里找不到段落 %r" % (tbl_path, sect))
        bad += 1
    else:
        j = text.find("\n### ", i + len(sect))
        block = text[i:j if j > 0 else len(text)]
        declared = _table_trigger_names(block)
        if declared != names:
            print("  X 权威表（%s %s）与清单不一致" % (tbl_path, sect))
            if names - declared:
                print("      清单有、表里没有: %s" % sorted(names - declared))
                print("        ⇒ 表漏了触发器。照表建出来的系统会缺保护。")
            if declared - names:
                print("      表里有、清单没有: %s" % sorted(declared - names))
                print("        ⇒ 要么清单漏登记，要么表里写了不存在的触发器。")
            bad += 1
        else:
            print("  OK 权威表 %s %s 与清单一致（%d 个触发器：%s）"
                  % (tbl_path, sect, want, ", ".join(sorted(names))))

        # ② 权威文件里的计数说法
        wrong = _wrong_claims(text, want)
        if wrong:
            print("  X %s 里有与清单不符的「N 个触发器」现状断言：" % tbl_path)
            for line, kind, num, sent in sorted(set(wrong)):
                print("      :%d  %s = %d，清单是 %d" % (line, kind, num, want))
                print("          %s" % sent)
            print("      若这句其实在讲历史/对比，加上「早期/曾经/实际只有」这类词，")
            print("      让它读起来就是历史 —— **别改数字**。")
            bad += 1
        else:
            print("  OK %s 里 %d 处计数说法都与清单一致（N=%d；历史对比陈述已排除）"
                  % (tbl_path, len(_count_claims(text)), want))

    # ③ 其它文档里的计数说法也必须对。
    #    ⚠️ 这一半曾经**比 ② 弱**（漏了第三个模式，于是「N 个触发器」这种最常见
    #       的写法根本没被匹配，看起来通过其实什么都没查）。现在两边共用同一份
    #       `_count_claims` / `_wrong_claims` —— 两处规则不一致，就是下一块盲区。
    for f in ("SKILL.md", "README.md"):
        t = _read_text(f)
        if t is None:
            continue
        claims = _count_claims(t)
        wrong = _wrong_claims(t, want)
        if wrong:
            print("  X %s 里的触发器计数与清单不符：" % f)
            for line, kind, num, sent in sorted(set(wrong)):
                print("      :%d  %s = %d（清单 %d）" % (line, kind, num, want))
                print("          %s" % sent)
            bad += 1
        elif claims:
            print("  OK %s 的 %d 处计数说法一致" % (f, len(claims)))

    return bad


def main():
    # ⚠️ 与其它检查脚本一致：某些控制台默认编码不是 UTF-8，
    #    print 里的 ✔/✖/⚠ 会抛 UnicodeEncodeError 让调用方误判失败。
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    doc = load_inventory()
    if doc is None:
        return 1

    print("  清单: %s" % INV)
    bad = 0

    print("  ── 清单自身 ──")
    bad += check_internal(doc)

    print("  ── 清单 ↔ 源码 ──")
    bad += check_source(doc)

    print("  ── 清单 ↔ 文档 ──")
    bad += check_docs(doc)

    print()
    if bad:
        print("  !! 部署事实三方不自洽 —— 这正是「文档写了、线上没有」的温床。")
        print("     ⚠️ 通过本步**不等于**线上正确：它只证明三方一致。")
        print("        线上回读见 scripts/verify-deploy.py（需要凭据，部署时跑）。")
        return 1
    print("  OK 清单 ↔ 文档 ↔ 源码三方自洽")
    print("     ⚠️ 这不证明线上状态。线上回读要跑 scripts/verify-deploy.py。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
