#!/bin/sh
export PATH=/bin:/sbin:/usr/bin:/usr/sbin TERM=linux LC_ALL=C HOME=/root
export PYTHONDONTWRITEBYTECODE=1
export PS1='RAM-RESCUE# '
umask 077
cd /root || exit 1
/bin/busybox cat /etc/motd
exec /bin/busybox sh -i
