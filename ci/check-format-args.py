#!/usr/bin/env python3
"""静态检查 `%` 格式化：占位符个数必须与实参个数一致。

    python3 ci/check-format-args.py

**为什么需要它。** 这类不匹配**只在运行时炸**，而且炸的地方往往正是告警路径：

    body = ("【实例】%s (%s)\\n" "【地域】%s / %s\\n" ...) % (a, b, c, d, ...)

少一个 `%s`、或多一个实参，`py_compile` 通过、`sh -n` 通过、`node --check` 通过、
导入也通过 —— 直到那一行真的被执行，抛 `TypeError: not enough arguments for
format string`（或 `not all arguments converted`）。

如果那行在 `main()` 开头（比如「守护已启动」那条推送），进程会在启动 2 秒内死掉，
而 OpenRC 的 `supervise-daemon` 会**无限重启**它：`rc-service status` 一直显示
started，磁盘上却没有任何状态写入 —— 正是本仓库反复强调的那类静默失效。

**判据**：只查**字面量**格式串（含相邻字面量隐式拼接）配元组实参这一种形态；
变量格式串、`%` 配 dict 的都跳过（查不了，也不该猜）。
"""
import ast
import io
import os
import re
import sys

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".playwright-mcp"}
# ⚠️ 必须把 `%%`（转义百分号）**先匹配掉再计数** —— 否则会拿它后面那个 `%`
#    去配 `[-#0 +]` 里的空格、再配后面的普通字母，凭空多算一个占位符。
#    所以这里把 `%%` 也写进备选分支，计数时只认真正的那一支。
FORMAT_TOKEN = re.compile(r"%%|(%[-#0 +]*\d*(?:\.\d+)?[hlL]?[diouxXeEfFgGcrsa])")


def count_placeholders(fmt):
    return sum(1 for m in FORMAT_TOKEN.finditer(fmt) if m.group(1) is not None)


def literal_string(node):
    """把「相邻字面量拼接」还原成一个字符串；不是纯字面量就返回 None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = literal_string(node.left)
        right = literal_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def check(path):
    try:
        tree = ast.parse(io.open(path, encoding="utf-8").read())
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod)):
            continue
        fmt = literal_string(node.left)
        if fmt is None:
            continue
        n = count_placeholders(fmt)
        rhs = node.right
        if isinstance(rhs, ast.Tuple):
            given = len(rhs.elts)
        elif isinstance(rhs, ast.Dict):
            continue          # %(name)s 形态，另论
        else:
            given = 1
        if n != given:
            out.append((node.lineno, n, given,
                        fmt.replace("\n", " ")[:60]))
    return out


def main():
    root = "."
    bad = 0
    files = 0
    for dp, dn, fns in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for fn in fns:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dp, fn)
            files += 1
            for lineno, n, given, sample in check(p):
                print("X %s:%d  占位符 %d 个，实参 %d 个" % (p, lineno, n, given))
                print("    格式串：%s…" % sample)
                bad += 1
    print()
    print("scanned %d files." % files)
    if bad:
        print("!! %d 处 %%-格式化不匹配 —— 运行时才会炸，且往往炸在告警路径上。" % bad)
        return 1
    print("OK  所有字面量 %-格式化的占位符与实参个数一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
