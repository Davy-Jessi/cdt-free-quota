#!/bin/sh
# 日志自限制：单文件超过 LIMIT 则截断，仅保留最后 KEEP 字节
# 由 /etc/periodic/15min 每 15 分钟调用（crond 已在运行）
#
# 部署位置：/usr/local/bin/logcap
# 挂载方式：ln -sf /usr/local/bin/logcap /etc/periodic/15min/logcap
#          （crontab 已有 `*/15 * * * * run-parts /etc/periodic/15min`）
# ⚠️ 本文件是**文档版** —— 部署到 /usr/local/bin/logcap 的实际内容可能与它不同。
#    别拿它做哈希校验。
#
# ── 存在理由 ─────────────────────────────────────────────────────────
#   * sing-box 在 warn 级别下把 ERROR 写到 stderr -> /var/log/sing-box.err。
#     被外部扫描（443 是全网扫描最频繁的端口）时，实测会出现突发写入，
#     按实测速率可达 40+ MB/天 —— 足以撑爆 1GB 系统盘。
#   * 保守窗口取 15 分钟（而非 1 小时），避免两次清理之间涨太多。
#   * 正常速率参考（实测）：
#       cdtguard.log  ~9 KB/天
#       messages      ~75 KB/天
#       sing-box.err  空闲时≈0，被扫描时可达数十 MB/天
#
# ── 为什么不装 logrotate ─────────────────────────────────────────────
#   crond 本来就在跑，/etc/periodic/15min 是 Alpine 自带机制，
#   零新依赖、零额外常驻进程，比装 logrotate 更省。
#
# ── 注意 ─────────────────────────────────────────────────────────────
#   这里必须同时覆盖 sing-box.err 和 sing-box.log：
#   服务定义里 output_log=/var/log/sing-box.log（stdout，基本为空），
#   error_log=/var/log/sing-box.err（stderr，**真正的日志在这**）。
#   曾经只盯了 .log 而漏掉 .err —— 那等于完全没保护。
LIMIT=1048576     # 1 MB
KEEP=131072       # 保留最后 128 KB
for f in /var/log/sing-box.err /var/log/sing-box.log /var/log/messages /var/log/cdtguard.log; do
  [ -f "$f" ] || continue
  SZ=$(stat -c %s "$f" 2>/dev/null || echo 0)
  if [ "$SZ" -gt "$LIMIT" ]; then
    tail -c "$KEEP" "$f" > "$f.tmp" 2>/dev/null && cat "$f.tmp" > "$f" && rm -f "$f.tmp"
    logger -t logcap "truncated $f: $SZ -> $(stat -c %s "$f")"
  fi
done
