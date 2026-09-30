#!/usr/bin/env python3
"""check-names.py —— 静态查「模块级用到但没定义」的名字。

为什么需要它：py_compile 只查语法；导入只执行模块级代码。
两者都**查不出**「只在某个函数体里用到、但从未定义」的名字 ——
而这类名字恰恰最容易漏（凭据常量、元数据辅助函数、一批顶层常量）。
"""
import ast
import builtins
import pathlib
import sys

BUILTINS = set(dir(builtins))


def collect_bound(tree):
    """收集所有"被绑定"的名字：模块级赋值/函数/类/import，以及任何赋值目标与形参。"""
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = node.args
                for arg in list(a.args) + list(a.posonlyargs) + list(a.kwonlyargs):
                    bound.add(arg.arg)
                if a.vararg:
                    bound.add(a.vararg.arg)
                if a.kwarg:
                    bound.add(a.kwarg.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for al in node.names:
                bound.add((al.asname or al.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, ast.comprehension):
            for t in ast.walk(node.target):
                if isinstance(t, ast.Name):
                    bound.add(t.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
    return bound


def report_unused(tree, path, ignore=()):
    """反方向：模块级 def 了但从没被引用过的名字。

    为什么需要：有一类失效 `py_compile` 抓不到 —— 函数的**定义**写进了文件，
    但主循环里的调用漏了。py_compile / 真导入 / 上面的未定义名检查全都查不出，
    结果是那段代码一直没被调用 —— 凭据过期后每次 API 调用都失败（403），
    """
    defined = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined[node.name] = node.lineno
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            pass
    unused = [(ln, n) for n, ln in defined.items()
              if n not in used and n not in ignore and True]
    for ln, n in sorted(unused):
        print("  ⚠ %s:%d  定义了但从未被引用: %s" % (path, ln, n))
    return unused


def main():
    # ⚠️ 与 check-disclosure.py 同样的防护：某些控制台的默认编码不是 UTF-8，
    #    print 里的 ✔/✖/⚠ 会抛 UnicodeEncodeError 并让调用方误判失败。
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    path = sys.argv[1]
    src = pathlib.Path(path).read_text(encoding="utf-8")
    tree = ast.parse(src)
    bound = collect_bound(tree) | BUILTINS | {"__name__", "__file__", "__doc__"}
    used = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.setdefault(node.id, node.lineno)
    missing = sorted((ln, n) for n, ln in used.items() if n not in bound)
    if missing:
        for ln, n in missing:
            print("  ✖ %s:%d  未定义的名字: %s" % (path, ln, n))
        return 1
    print("  ✔ %s：无未定义名（检查了 %d 个载入名）" % (path, len(used)))
    if report_unused(tree, path):
        return 1   # 部署闸门：有「定义了但从不被调用」的函数就不放行
    return 0


if __name__ == "__main__":
    sys.exit(main())
