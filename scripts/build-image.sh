#!/bin/sh
# 构建可启动的 Alpine 磁盘镜像（1 GiB）。
#
#   用法: sudo build-image.sh [输出路径] [Alpine 版本]
#   例:   sudo ./build-image.sh disk.raw 3.24.2
#
# 为什么需要自建镜像：**公共镜像全部 ≥ 20 GB**，而本方案的系统盘只要 1 GB
# （按量付费省钱）。用公共镜像会报 `InvalidSystemDiskSize.LessThanImageSize`。
#
# ⚠️ 为什么不用「Alpine ISO + VNC 手动 setup-alpine」那条路：
#    VNC 是 canvas 渲染，脚本化困难。本脚本直接构建可启动磁盘镜像，
#    **全程不需要 VNC**。
#
# ⚠️ 本脚本**未经端到端实测**（需要一台干净的 Linux 才能真跑一遍）。
#    它是对一份已验证过的**手工步骤**的机械化重写。首次运行请留意每一步的输出，
#    尤其是 chroot 那步（minirootfs 里 apk 不在 PATH，必须给全路径）。
#
# 依赖：syslinux / extlinux（Debian: apt install syslinux extlinux；
#       Alpine: apk add syslinux）
# 需要：root + loop 设备 + 能联网（chroot 里要 apk add）
set -e

OUT="${1:-disk.raw}"
# ⚠️ 默认版本**会过期**：写死一个小版本号，过一阵子它就成了「该分支的旧补丁」，
#    再往后整个分支都会 EOL。所以传参优先，默认值只求「当下可用」。
#    选版依据（2026-09-30 核对）：3.24 是当时的稳定分支。
#    ⚠️ **用之前去 https://alpinelinux.org/releases/ 看一眼当前稳定分支与 EOL 日期**，
#       别照抄这里的数字。
VER="${2:-3.24.2}"
SIZE_MB=1024
MIRROR="https://dl-cdn.alpinelinux.org/alpine/v${VER%.*}/releases/x86_64"
TARBALL="alpine-minirootfs-${VER}-x86_64.tar.gz"
ROOTFS="rootfs.$$"

say() { printf '\n== %s ==\n' "$1"; }
cleanup() {
  [ -n "$LOOP" ] && { umount "$ROOTFS" 2>/dev/null || true; losetup -d "$LOOP" 2>/dev/null || true; }
  rmdir "$ROOTFS" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
LOOP=""

[ "$(id -u)" = "0" ] || { echo "X 需要 root（loop 设备 + chroot）"; exit 1; }

# 前置工具检查。**在动手之前一次性报全**，而不是做到一半才发现缺东西 ——
# 这个脚本会建 loop 设备、挂载、chroot，中途失败会留下挂着的设备要手工收拾。
MISSING=""
for c in sfdisk losetup mkfs.ext4 extlinux chroot tar; do
  command -v "$c" >/dev/null 2>&1 || MISSING="$MISSING $c"
done
command -v wget >/dev/null 2>&1 || command -v curl >/dev/null 2>&1 || MISSING="$MISSING wget/curl"
if [ -n "$MISSING" ]; then
  echo "X 缺少:$MISSING"
  echo "  Debian/Ubuntu: apt install syslinux extlinux"
  echo "  Alpine:        apk add syslinux"
  exit 1
fi

say "STEP 0 —— 取 minirootfs"
if [ ! -f "$TARBALL" ]; then
  echo "  下载 $TARBALL"
  if command -v wget >/dev/null 2>&1; then
    wget -q -O "$TARBALL" "$MIRROR/$TARBALL"
  else
    curl -fsSL -o "$TARBALL" "$MIRROR/$TARBALL"
  fi
fi
echo "  $TARBALL  $(wc -c < "$TARBALL") 字节"

say "STEP 1 —— 建 ${SIZE_MB} MiB raw 盘 + MBR 分区"
rm -f "$OUT"
dd if=/dev/zero of="$OUT" bs=1M count="$SIZE_MB" status=none
# 2048 起（1 MiB 对齐），83 = Linux，* = 可启动
sfdisk "$OUT" >/dev/null <<'EOF'
label: dos
2048,,83,*
EOF
echo "  已建 $OUT"

say "STEP 2 —— 挂载并解压"
LOOP=$(losetup --find --show --partscan "$OUT")
echo "  loop: $LOOP"
mkfs.ext4 -q -F "${LOOP}p1"
mkdir -p "$ROOTFS"
mount "${LOOP}p1" "$ROOTFS"
tar -xzf "$TARBALL" -C "$ROOTFS"
echo "  解压完成"

say "STEP 3 —— chroot 装内核与 sshd"
# ⚠️⚠️ **`python3` 是必需的** —— 守护（`cdtguard.py`）就是 Python，
#     而 Alpine minirootfs 默认**不带** python3。
#     ⚠️ apk 清单里**漏了它**的话，按本文档建出来的机器跑不了守护，
#        `deploy-cdtguard.sh` 的 STEP 1.5 就会 `set -e` 中止。
# ⚠️ 必须给全路径 /sbin/apk —— minirootfs 里 apk 不在默认 PATH
chroot "$ROOTFS" /bin/sh -c 'PATH=/sbin:/bin:/usr/sbin:/usr/bin \
  apk update && apk add alpine-base linux-virt openssh openssh-client mkinitfs \
  python3'

say "STEP 4 —— 基础配置"
# ⚠️ 示例主机名 —— 别照抄成你线上机器的名字（只允许字母/数字/连字符）。
printf '%s\n' "${IMAGE_HOSTNAME:-cdt-node}" > "$ROOTFS/etc/hostname"
cat > "$ROOTFS/etc/fstab" <<'EOF'
/dev/vda1  /  ext4  defaults,noatime  0 1
EOF
cat > "$ROOTFS/etc/network/interfaces" <<'EOF'
auto lo
iface lo inet loopback

auto eth0
iface eth0 inet dhcp
EOF
chroot "$ROOTFS" /bin/sh -c 'rc-update add sshd default; rc-update add networking default; rc-update add hostname default'
# SSH：禁用密码登录（只允许公钥）。把公钥放进 authorized_keys 再往下走。
if [ -n "$SSH_PUBKEY" ]; then
  mkdir -p "$ROOTFS/root/.ssh"
  printf '%s\n' "$SSH_PUBKEY" > "$ROOTFS/root/.ssh/authorized_keys"
  chmod 700 "$ROOTFS/root/.ssh"; chmod 600 "$ROOTFS/root/.ssh/authorized_keys"
  echo "  已写入 SSH 公钥"
else
  echo "  ⚠ 未设置 SSH_PUBKEY —— 镜像里没有任何可登录的凭据。"
  echo "    设置方式: SSH_PUBKEY=\"\$(cat ~/.ssh/id_ed25519.pub)\" $0 $OUT"
fi
sed -i 's/^#*PermitRootLogin.*/PermitRootLogin prohibit-password/' "$ROOTFS/etc/ssh/sshd_config"

say "STEP 5 —— 引导配置"
cat > "$ROOTFS/boot/extlinux.conf" <<'EOF'
DEFAULT alpine
PROMPT 0
TIMEOUT 20
LABEL alpine
    LINUX /boot/vmlinuz-virt
    INITRD /boot/initramfs-virt
    APPEND root=/dev/vda1 modules=ext4,virtio_blk,virtio_pci,virtio_net,virtio_scsi,virtio_console console=tty0 console=ttyS0,115200 quiet
EOF
extlinux --install "$ROOTFS/boot"
# syslinux 的模块路径因发行版而异，两个常见位置都试
for d in /usr/lib/syslinux/modules/bios /usr/share/syslinux; do
  [ -d "$d" ] && cp "$d"/*.c32 "$ROOTFS/boot/" 2>/dev/null && echo "  已复制 .c32 模块（来自 $d）" && break
done
# MBR 引导码同样
for f in /usr/lib/syslinux/mbr/mbr.bin /usr/share/syslinux/mbr.bin; do
  [ -f "$f" ] && dd if="$f" of="$OUT" bs=440 count=1 conv=notrunc status=none && echo "  已写入 MBR（来自 $f）" && break
done

say "STEP 6 —— 卸载"
umount "$ROOTFS"
losetup -d "$LOOP"; LOOP=""
rmdir "$ROOTFS"
echo "  完成：$OUT  $(wc -c < "$OUT") 字节  sha256=$(sha256sum "$OUT" | cut -c1-16)"
echo
echo "  下一步: ./import-image.sh $OUT"
