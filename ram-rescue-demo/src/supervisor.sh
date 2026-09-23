#!/bin/sh
export PATH=/bin:/sbin:/usr/bin:/usr/sbin TERM=linux LC_ALL=C
export LOGIN_TIMEOUT=0
unset ENV BASH_ENV PYTHONPATH PYTHONHOME LD_PRELOAD LD_LIBRARY_PATH
trap ':' INT QUIT TSTP
while :; do
    /bin/busybox stty sane
    /bin/busybox cat /etc/issue
    /bin/busybox login
    /bin/busybox sleep 2
done
