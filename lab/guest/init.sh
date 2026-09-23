#!/bin/sh
set -eu
export PATH=/bin:/sbin:/usr/bin:/usr/sbin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mount -t tmpfs -o noswap tmpfs /run
mkdir -p /run/lock/lvm /run/lvm
for module in xhci_pci usb_storage uas sd_mod dm_mod dm_multipath dm_round_robin ext4 virtio_pci virtio_blk; do
    /sbin/modprobe "$module"
done
python3 /opt/lab/agent.py --setup || { sleep 2; exit 1; }
# Independent tools and control channels stay on tmpfs after switch_root.
mkdir -p /run/rescue
mount -t tmpfs -o noswap,size=256M tmpfs /run/rescue
for item in bin sbin usr lib lib64 etc opt; do
    [ ! -e "/$item" ] || cp -a "/$item" /run/rescue/
done
mkdir -p /run/rescue/dev /run/rescue/proc /run/rescue/sys /run/rescue/run /run/rescue/tmp /run/rescue/var/log
mount --bind /dev /run/rescue/dev
mount --bind /proc /run/rescue/proc
mount --bind /sys /run/rescue/sys
mount --bind /run /run/rescue/run
if ! grep -qw ram_rescue_ubuntu=1 /proc/cmdline; then
chroot /run/rescue python3 /opt/lab/agent.py &
if [ -f /run/rescue/etc/rescue/path-guard.json ]; then
    chroot /run/rescue python3 /opt/lab/path_guard.py &
fi
chroot /run/rescue /bin/sh -c 'exec /bin/setsid /bin/sh -i </dev/ttyS2 >/dev/ttyS2 2>&1' &
fi
for item in dev proc sys run; do mount --move "/$item" "/newroot/$item"; done
if grep -qw ram_rescue_ubuntu=1 /newroot/proc/cmdline; then
    exec switch_root /newroot /sbin/init
fi
exec switch_root /newroot /opt/lab/root-init.sh
