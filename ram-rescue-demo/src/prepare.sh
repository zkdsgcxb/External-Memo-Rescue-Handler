#!/bin/sh
set -eu
BASE=/usr/local/lib/ram-rescue-demo
RAM=/run/ram-rescue-demo
case "${1:-start}" in
stop)
    if mountpoint -q "$RAM"; then umount -R "$RAM"; fi
    rmdir "$RAM" 2>/dev/null || true
    exit 0
    ;;
start) ;;
*) exit 2 ;;
esac
[ "$(id -u)" = 0 ]
if mountpoint -q "$RAM"; then
    echo 'Existing rescue RAM mount found; refusing to overwrite it.' >&2
    exit 1
fi
[ "$(cat /sys/fs/cgroup/ramrescue.slice/memory.swap.max)" = 0 ] || {
    echo 'Required cgroup swap protection is missing.' >&2; exit 1;
}
cd "$BASE"
sha256sum -c rescue-root.sha256
mkdir -p "$RAM"
mount -t tmpfs -o size=256M,noswap,mode=0700,nosuid,nodev ram-rescue-demo "$RAM"
cleanup() { umount -R "$RAM" 2>/dev/null || true; }
trap cleanup EXIT
tar -xzf "$BASE/rescue-root.tar.gz" -C "$RAM" --no-same-owner
cp /etc/ram-rescue-demo/shadow "$RAM/etc/shadow"
chmod 600 "$RAM/etc/shadow"
mkdir -p /run/lock/lvm
mount --rbind /dev "$RAM/dev"
mount --make-rslave "$RAM/dev"
mount -t proc proc "$RAM/proc"
mount --bind /sys "$RAM/sys"
mount -o remount,bind,ro "$RAM/sys"
# Share the host's RAM-backed LVM locks: don't create a second lock universe.
mount --bind /run/lock/lvm "$RAM/run/lock/lvm"
chroot "$RAM" /bin/busybox sh -c 'test -r /etc/shadow; /usr/bin/python3 -I -c "import json,subprocess,select; print(\"RAM runtime ready\")"'
chroot "$RAM" /sbin/lvm dumpconfig --validate
findmnt -n -o OPTIONS "$RAM" | tr ',' '\n' | grep -qx noswap
trap - EXIT
