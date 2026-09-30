#!/usr/bin/env python3
"""比较两份**同源** Python 文件的**代码结构**，忽略注释、docstring 与常量值。

    python ci/compare-code.py <本仓库的文件> <你本地那份参考文件>

**为什么需要它。** 脱敏是一堆文本替换，而文本替换会**误伤代码**。
一个真实事故：给日期做脱敏时，正则把云 API 的 `Version` 参数也吃了 ——

    rpc("DescribeInstances", "2014-05-26", ...)      # 对的
    rpc("DescribeInstances", "<日期已隐去>", ...)     # 脱敏后，每一次调用都会失败

`py_compile` 通过、看起来也「像占位符」、diff 时人眼很容易滑过去 ——
但它让整个程序**一个 API 都调不通**。

**判据**：脱敏只应改变**注释、docstring、以及那几处凭据常量的值**。
函数集合、API 调用（动作名 + 版本 + 端点）、以及其余常量的值都**必须一致**。

⚠️ 这个脚本需要你把那份未脱敏的参考文件**留在本地**（它不该进仓库）。
   仓库里没有它时，本脚本无法运行 —— 这是设计上的取舍，不是缺陷。
   替代它的自动检查是 `ci/check-disclosure.py` 的 ③ 号规则
   （字符串字面量里出现非 `<YOUR_*>` 的占位符 = 事故）。
"""
import ast
import io
import sys

CRED = {"AK", "SK", "PUSH_TOKEN", "STS_TOKEN", "INSTANCE_ID", "EIP",
        "OSS_BUCKET", "BW_ID", "INST_NAME", "PUSH_URL"}


def profile(path):
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    funcs = sorted(n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    calls = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "rpc":
            # ⚠️ 这里的哨兵值刻意**不用尖括号** —— 尖括号留给 `<YOUR_*>` 占位符。
            #    仓库里有个检查规定「代码位置出现的尖括号串只允许是 <YOUR_*>」，
            #    用 (expr) 就不会把自己也钓出来。
            a = [x.value if isinstance(x, ast.Constant) else "(expr)" for x in n.args]
            calls.append(tuple(a[:2] + a[3:4]))          # (action, version, endpoint)
    consts = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
            if isinstance(n.value, ast.Constant) and isinstance(n.value.value, (int, float, str)):
                consts[n.targets[0].id] = n.value.value
    return funcs, sorted(calls), consts


def main():
    if len(sys.argv) < 3:
        print(__doc__.strip())
        return 2
    a, b = sys.argv[1], sys.argv[2]
    try:
        fa, ca, ka = profile(a)
        fb, cb, kb = profile(b)
    except (OSError, SyntaxError) as e:
        print("X 读不了或解析不了: %s" % e)
        return 2

    bad = 0
    print("  函数数: %s=%d   %s=%d" % (a, len(fa), b, len(fb)))
    if fa != fb:
        for f in sorted(set(fa) ^ set(fb)):
            print("  X 函数只在一边: %s" % f)
        bad += 1
    else:
        print("  OK 函数集合一致")

    print("  API 调用 (action, version, endpoint): %s=%d   %s=%d" % (a, len(ca), b, len(cb)))
    if ca != cb:
        for c in ca:
            if c not in cb:
                print("  X 只在 %s: %s" % (a, c))
        for c in cb:
            if c not in ca:
                print("  X 只在 %s: %s" % (b, c))
        bad += 1
    else:
        print("  OK API 调用完全一致 —— **这一项才是关键**（版本号被误伤会在这里现形）")

    diffs = [(k, ka[k], kb[k]) for k in sorted(set(ka) & set(kb))
             if k not in CRED and ka[k] != kb[k]]
    if diffs:
        for k, va, vb in diffs:
            print("  X 常量 %s: %s=%r  %s=%r" % (k, a, va, b, vb))
        bad += 1
    else:
        print("  OK 常量无非预期差异（凭据类常量已排除）")

    print()
    if bad:
        print("!! 两份文件**代码结构不一致** —— 脱敏很可能改坏了东西。")
        return 1
    print("两份文件代码结构一致（差异仅限注释 / docstring / 凭据常量）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
