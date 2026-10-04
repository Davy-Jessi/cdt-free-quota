#!/usr/bin/env python3
"""verify-deploy.py —— **回读线上实际状态**，与 inventory.json 逐条比对。

    python3 scripts/verify-deploy.py            # 只读，不改任何东西
    python3 scripts/verify-deploy.py --quiet    # 进度静默；**失败照常打印**

**为什么需要它（真实事故）。**

`scripts/provision/README.md` §5.3 明确列了 **3 个**触发器，还专门警告
`guard-liveness-8min`「**别漏掉** —— 它是唯一能在护闸悄悄死掉时告警的机制」。
但线上**只有 2 个**：那个触发器从来没被创建过。

`ci/check-inventory.py` 只能证明「清单 ↔ 文档 ↔ 源码」三者自洽 —— 那是
**必要条件，不是充分条件**。文档可以三处都写对，而线上依然是错的。
**这一步才是去线上数一遍。** 少了它，「文档写了」就仍然只是「写了」。

判据（逐条，任一不符即失败并给出**具体差异**）：

  ① 每个声明的触发器**都真的存在**（名字逐一回读，不靠数量推断）
  ② 类型对得上（timer / eventbridge）
  ③ timer 的 cron 对得上（**cron 写错 = 周期错**，而功能看起来还在）
  ④ 启用状态对得上（**「存在但被停用」与「不存在」是同一种失效**）
  ⑤ 存活巡检的 payload 真的带 `liveness`（漏了 ⇒ 每 8 分钟刷一条报告）
  ⑥ 函数不带任何**未声明**的触发器（多出来的也是漂移）
  ⑦ runtime / handler 对得上
  ⑧ 声明的 env 键**都在**（判据方向是「线上 ⊇ 声明」，因为多传不算错）
  ⑨ 函数返回的 `ver` 等于源码里的 `const VER`（证明跑的是这份代码）

⚠️⚠️ **本脚本绝不打印度量值**：env 只比**键名**，不读值、不打印值。
   生产凭据不该出现在任何输出里（曾因 `GetFunction` 明文回显而暴露过）。

**结构**：`compare()` 是**纯函数**（清单 + 线上视图 → 失败清单），不碰网络、
不 import SDK。`main()` 只负责从 SDK 取数、把结果打出来。
这样 `ci/smoke-verify-deploy.py` 能在**没有任何凭据、也不装 SDK**的 CI 里
把比对逻辑真的跑一遍 —— 因为一个「不会报警的验证器」和没有验证器是一样的，
而它恰好也是本仓库栽过的那类坑。

凭据：ALIYUN_AK / ALIYUN_SK / ALIYUN_UID（部署期身份，用完即摘）。
"""
import json
import os
import pathlib
import re
import sys

QUIET = "--quiet" in sys.argv


def say(msg):
    """**进度**输出 —— `--quiet` 下不打印。

    ⚠️⚠️ **失败信息绝不要走这里。** 本脚本里只有一个函数受 `--quiet` 影响，
       就是它；其余一律用裸 `print()`（永远打印）。
       曾经这里还有一个 `bad()` 专门给失败用，但它是裸 `print()` 的同义词，
       最后没人调用 —— `ci/check-names.py` 把它抓了出来。
       两个名字干同一件事，会让人以为裸 `print()` 也是被门控的。所以删掉了。
       **规矩：只有 `say()` 会被静默，失败一律裸 `print()`。**
       （「静默跳过」与「静默失败」是两件事，本仓库在这一点上栽过。）
    """
    if not QUIET:
        print(msg)


def parse_config(raw):
    """把 trigger_config 解析成 dict。解析不了返回 None。

    ⚠️ `Trigger.trigger_config` 在 SDK 里是**字符串**（不是解析好的对象），
       要自己 json.loads。这与 fc-keeper 头部那条「定时器的 payload 是字符串」
       是同一类坑，只是发生在不同的层。
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        got = json.loads(raw)
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


def compare(decl, view):
    """纯函数：清单 vs 线上视图 → 失败清单。

    `view` 的形状（由 main() 从 SDK 组装，测试里用固定 fixture）：
        {
          "runtime": str, "handler": str,
          "env_keys": set[str],
          "triggers": {name: {"type": str, "config_raw": str}},
          "ver": str | None,
          "ver_error": str | None,
        }

    返回 `[{"msg": str, "hint": [str, ...]}, ...]`；空列表 = 通过。
    """
    fails = []
    fcdecl = decl.get("fc") or {}
    want_triggers = {t["name"]: t for t in (fcdecl.get("triggers") or [])}
    want_env = set(fcdecl.get("requiredEnv") or [])
    live = view.get("triggers") or {}

    # ⑦ runtime / handler
    want_rt = fcdecl.get("runtime")
    if want_rt and view.get("runtime") != want_rt:
        fails.append({"msg": "runtime 不符：线上 %s，清单 %s"
                             % (view.get("runtime"), want_rt), "hint": []})
    want_h = fcdecl.get("handler")
    if want_h and view.get("handler") != want_h:
        fails.append({"msg": "handler 不符：线上 %s，清单 %s"
                             % (view.get("handler"), want_h), "hint": []})

    # ⑧ env 键（**只比键名**）
    live_env = set(view.get("env_keys") or [])
    missing_env = sorted(want_env - live_env)
    if missing_env:
        fails.append({
            "msg": "线上缺这些 env 键：%s" % missing_env,
            "hint": ["FC 的 environment_variables 是**整体覆盖**语义 ——",
                     "少一个键 = 删掉一个键。缺 ALIYUN_SK 之类的后果是**静默断链**。"],
        })

    # ① 声明的都必须存在
    for name, spec in want_triggers.items():
        if name not in live:
            hint = []
            if spec.get("payloadLiveness"):
                hint = ["这是 dead-man's switch —— 没有它，「护闸悄悄死掉」",
                        "就**再没有任何东西会告诉你**。当初线上就是缺这一个。"]
            fails.append({"msg": "触发器**不存在**: %s（%s）"
                                 % (name, spec.get("role")), "hint": hint})

    # ⑥ 未声明的也必须报 —— 多出来的同样是漂移
    for name in sorted(set(live) - set(want_triggers)):
        fails.append({"msg": "触发器**未在清单里声明**: %s"
                             "（线上有、inventory.json 没有）" % name, "hint": []})

    # ② ③ ④ ⑤ 逐个比对
    for name, spec in want_triggers.items():
        t = live.get(name)
        if t is None:
            continue
        if spec.get("type") and t.get("type") != spec["type"]:
            fails.append({"msg": "%s 类型不符：线上 %s，清单 %s"
                                 % (name, t.get("type"), spec["type"]), "hint": []})

        cfg = parse_config(t.get("config_raw"))
        if cfg is None:
            fails.append({"msg": "%s 的 triggerConfig 不是合法 JSON：%r"
                                 % (name, str(t.get("config_raw"))[:80]), "hint": []})
            continue

        # ③ cron
        want_cron = spec.get("cron")
        if want_cron and cfg.get("cronExpression") != want_cron:
            fails.append({
                "msg": "%s 的 cron 不符：线上 %r，清单 %r"
                       % (name, cfg.get("cronExpression"), want_cron),
                "hint": ["cron 写错时功能**看起来还在**（只是周期不对），",
                         "所以这一项必须显式比对，不能靠「触发器存在」了事。"],
            })

        # ④ 启用状态 —— 「存在但被停用」与「不存在」是同一种失效。
        #     ⚠️ 键名随类型而变：timer 用 `enable`，eventbridge 用 `triggerEnable`。
        #        只认一个的话，另一种类型会**永远读不到值**、
        #        然后被当成「没这个字段」悄悄放过。
        if spec.get("enabled") is not None:
            key = "enable" if spec.get("type") == "timer" else "triggerEnable"
            got_en = cfg.get(key)
            if got_en is None:
                fails.append({
                    "msg": "%s 的 triggerConfig 里没有 %r 字段（读不到启用状态）"
                           % (name, key),
                    "hint": ["读不到 ≠ 启用。这条算失败，否则就是静默放过。"],
                })
            elif bool(got_en) != bool(spec.get("enabled")):
                fails.append({
                    "msg": "%s 的启用状态不符：线上 %s，清单 %s"
                           % (name, bool(got_en), spec.get("enabled")),
                    "hint": ["触发器**在列表里看得见，但一次都不会触发**。",
                             "这和不存在的后果一样，却更难发现 —— 因为列表看起来是齐的。"],
                })

        # ⑤ 存活巡检的 payload 必须真的带 liveness
        if spec.get("payloadLiveness"):
            payload = cfg.get("payload")
            has = False
            if isinstance(payload, str):
                try:
                    has = bool(json.loads(payload).get("liveness"))
                except ValueError:
                    has = False
            elif isinstance(payload, dict):
                has = bool(payload.get("liveness"))
            if not has:
                fails.append({
                    "msg": "%s 的 payload 里没有 liveness：%r" % (name, payload),
                    "hint": ["fc-keeper 会把它当成「每日巡检」⇒ **每 8 分钟推一条报告**",
                             "（约 180 条/天）。代价是刷屏，不是失明 —— 但仍是错的。"],
                })

    # ⑨ ver
    vs = fcdecl.get("verSource")
    expect = None
    if vs and (pathlib.Path(vs)).exists():
        m = re.search(r"const VER = '([^']+)'",
                      (pathlib.Path(vs)).read_text(encoding="utf-8"))
        expect = m.group(1) if m else None
    if expect:
        if view.get("ver_error"):
            # ⚠️ 「读不到」不等于「读到了且一致」。本仓库的 deploy-fc.py 在
            #    实例不是 Running 时**拒绝**悄悄 exit 0，理由是同一条：
            #    **不校验 ≠ 校验通过**。
            fails.append({
                "msg": "无法回读 ver：%s" % view["ver_error"],
                "hint": ["不校验 ≠ 校验通过。所以这里算失败。",
                         "若确实接受不校验，应当**显式**说明，而不是让它静默通过。"],
            })
        elif view.get("ver") != expect:
            fails.append({
                "msg": "ver 不符：函数返回 %r，源码是 %r ⇒ 线上跑的不是这份代码"
                       % (view.get("ver"), expect),
                "hint": ["多半是热实例仍在跑旧代码（UpdateFunction 之后需要重试）。"],
            })
    return fails


def require_env(name):
    v = os.environ.get(name)
    if not v:
        print("X 缺少环境变量 %s（部署期凭据，不是运行时角色）" % name)
        print("  见 .env.example 第三组。")
        sys.exit(2)
    return v


def load_inventory():
    p = pathlib.Path("inventory.json")
    if not p.exists():
        print("X 找不到 inventory.json（要在仓库根跑）")
        sys.exit(2)
    return json.loads(p.read_text(encoding="utf-8"))


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    # ⚠️ SDK 的 import 放在 main() 里，不放模块级 —— 这样
    #    ci/smoke-verify-deploy.py 能在**没装 SDK** 的 CI 里 import 本文件
    #    来测 compare()。（CI 不装第三方包，这是刻意的：供应链面越小越好。）
    #    与 deploy-fc.py 用同一套 SDK —— 刻意不另造一条 RPC 路径：
    #    两条路径会在「哪一条支持 ListTriggers」这种地方悄悄分叉。
    try:
        from alibabacloud_fc20230330.client import Client
        from alibabacloud_tea_openapi import models as oa
        from alibabacloud_fc20230330 import models as fc
    except ImportError as e:
        print("X 缺少 SDK: %s" % e)
        print("  pip install alibabacloud_fc20230330 alibabacloud_tea_openapi")
        return 2

    AK = require_env("ALIYUN_AK")
    SK = require_env("ALIYUN_SK")
    UID = require_env("ALIYUN_UID")
    REGION = os.environ.get("REGION_ID", "cn-hongkong")

    inv = load_inventory()
    fcdecl = inv.get("fc") or {}
    FN = fcdecl.get("functionName") or "cdt-keeper"

    c = Client(oa.Config(access_key_id=AK, access_key_secret=SK,
                         endpoint="%s.%s.fc.aliyuncs.com" % (UID, REGION)))

    # ── 读回函数本体 ──────────────────────────────────────────────────
    try:
        cur = c.get_function(FN, fc.GetFunctionRequest()).body
    except Exception as e:
        print("X 读不到函数 %s：%s" % (FN, e))
        print("  函数不存在？地域错？凭据没有 fc:GetFunction？")
        return 1
    say("  函数: %s  runtime=%s  handler=%s" % (FN, cur.runtime, cur.handler))

    # ── 读回触发器清单（**逐一按名字**，不靠数量推断）─────────────────
    # ⚠️ 数量相等不等于集合相等：删一个、加一个，数量照样对。
    #    而且「按数量判断」正是当初事故能溜过去的原因 —— 谁也没去数。
    #
    # ⚠️ 返回类型是 `ListTriggersResponse`，其 `.body` 是 `ListTriggersOutput`
    #    （**不是** `ListTriggersResponseBody` —— 第一版按 `GetFunctionResponseBody`
    #     的命名习惯猜了这个名字，实测不存在，会在运行时抛 AttributeError）。
    try:
        out = c.list_triggers(FN, fc.ListTriggersRequest()).body
        raw_triggers = out.triggers or []
    except Exception as e:
        print("X 读不到触发器清单：%s" % e)
        print("  ⚠️ 这一步**必须**能跑 —— 它正是当初漏掉 guard-liveness-8min 的地方。")
        print("     读不到就当失败，**不要**退化成「只查环境变量」。")
        return 1

    live = {t.trigger_name: {"type": t.trigger_type, "config_raw": t.trigger_config}
            for t in raw_triggers}
    say("  触发器: 线上 %d 个 —— %s" % (len(live), ", ".join(sorted(live))))

    # ── 回读 ver ───────────────────────────────────────────────────────
    ver, ver_error = None, None
    try:
        resp = c.invoke_function(FN, fc.InvokeFunctionRequest(
            body=json.dumps({"probe": "verify-deploy"})))
        b = resp.body
        if isinstance(b, str):
            raw = b
        elif hasattr(b, "read"):
            raw = b.read().decode()
        else:
            raw = b.decode()
        ver = json.loads(raw).get("ver")
        say("  ver=%s" % ver)
    except Exception as e:
        ver_error = str(e)
        say("  ⚠️ 调用失败：%s" % e)

    view = {
        "runtime": cur.runtime,
        "handler": cur.handler,
        "env_keys": set((cur.environment_variables or {}).keys()),
        "triggers": live,
        "ver": ver,
        "ver_error": ver_error,
    }

    fails = compare(inv, view)
    say("  env: 声明的 %d 个键%s"
        % (len(fcdecl.get("requiredEnv") or []),
           "都在" if not any("env 键" in f["msg"] for f in fails) else "有缺失"))

    print()
    if fails:
        print("X 线上状态与 inventory.json 不符（%d 条）：" % len(fails))
        for i, f in enumerate(fails, 1):
            print("  %d) %s" % (i, f["msg"]))
            for h in f["hint"]:
                print("       %s" % h)
        print()
        print("  怎么修：")
        print("    · 触发器缺失/写错 —— 见 scripts/provision/README.md §5.3 的 CreateTrigger")
        print("    · 是你**故意**改了线上 —— 那就同步改 inventory.json（并提交）")
        print("  ⚠️ 不要为了让本脚本变绿而删掉清单里的声明。")
        print("     清单少一条，保护就少一层，而**没有任何东西会再告诉你**。")
        return 1

    print("✔ 线上状态与 inventory.json 一致（%d 个触发器、%d 个 env 键、ver=%s）"
          % (len(fcdecl.get("triggers") or []), len(fcdecl.get("requiredEnv") or []),
             ver or "?"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
