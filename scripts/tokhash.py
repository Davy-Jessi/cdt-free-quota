# ⚠️⚠️ 使用前必读
#
# 1) **两个文件必须在同一台机器上。** 本工具直接读两个路径，不做任何传输。
#    如果你的本地机器与部署机之间没有共享文件系统，**这个工具用不了** ——
#    不是配置问题，是它按设计就只做本地比对。
#
# 2) **脱敏与 token 等价对同一个文件是互斥的。**
#    比对的是 (token 类型, token 字符串)，**字符串内容也算**。
#    所以把真值换成 `<YOUR_XXX>` 占位符，token 流必然不同 ——
#    **一份脱敏过的副本永远不可能与线上文件 token 等价。**
#
#    推论：脱敏会改变 token 流，所以本工具只适用于**两份都未脱敏**的同源文件。
#
# 3) 在「本地机器与部署机不互通」的环境下，**实际有效的替代是静态检查**：
#      check-names.py   载入但未绑定 / 定义了但从未被引用
#    适用场景：函数的**定义**写进了文件、主循环里的**调用**漏了 ——
#    静态检查能抓；而本工具要求两个文件同机，那种形态下往往用不上。
#

import sys, tokenize, hashlib


def norm(path):
    """词法规范化：去掉注释/空行/编码声明，再剔除模块级 docstring。
    只保留 (token 类型, 原文)，因此跨 Python 版本稳定。"""
    toks = []
    with open(path, "rb") as f:
        for tok in tokenize.tokenize(f.readline):
            if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.ENCODING):
                continue
            toks.append((tok.type, tok.string))
    # 剔除第一个有意义的 token（若它是 STRING，即模块 docstring）
    for i, (tt, ts) in enumerate(toks):
        if tt in (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
            continue
        if tt == tokenize.STRING:
            del toks[i]
        break
    return toks


a = norm(sys.argv[1])
print("TOKENS %d" % len(a))
print("TOKSHA %s" % hashlib.sha256(repr(a).encode()).hexdigest())

if len(sys.argv) > 2:
    b = norm(sys.argv[2])
    print("B_TOKENS %d" % len(b))
    print("VERDICT %s" % ("EQUAL" if a == b else "DIFFER"))
    if a != b:
        shown = 0
        for i in range(min(len(a), len(b))):
            if a[i] != b[i]:
                print("  diff@%d  A=%r  B=%r" % (i, a[i], b[i]))
                shown += 1
                if shown >= 8:
                    break
        if shown == 0:
            print("  前缀相同，长度不同：A 多 %d" % (len(a) - len(b)))
