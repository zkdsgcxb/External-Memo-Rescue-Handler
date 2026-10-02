#!/bin/sh
export PATH=/bin:/sbin:/usr/bin:/usr/sbin TERM=linux LC_ALL=C HOME=/root
export PYTHONDONTWRITEBYTECODE=1
export PS1='RAM-RESCUE# '
unset ENV BASH_ENV PYTHONPATH PYTHONHOME LD_PRELOAD LD_LIBRARY_PATH

# This file is data, never shell code. A missing or malformed policy must not
# silently turn an authenticated session into one without its idle limit.
reject_policy() { echo 'Invalid rescue session policy; refusing shell.' >&2; exit 1; }
idle_timeout=
while IFS='=' read -r key value || [ -n "$key$value" ]; do
    case "$key" in
        '') [ -z "$value" ] || reject_policy; continue ;;
        '#'*) continue ;;
        IDLE_TIMEOUT)
            [ -z "$idle_timeout" ] || reject_policy
            case "$value" in ''|*[!0-9]*) reject_policy ;; esac
            [ "$value" = 0 ] || [ "${value#0}" = "$value" ] || reject_policy
            [ "${#value}" -le 5 ] && [ "$value" -le 86400 ] || reject_policy
            idle_timeout=$value
            ;;
        *) reject_policy ;;
    esac
done < /etc/rescue/session.conf
[ -n "$idle_timeout" ] || reject_policy
export TMOUT=$idle_timeout
# Do not persist commands in this full-root rescue terminal.
export HISTFILE=/dev/null
umask 077
cd /root || exit 1
/bin/busybox cat /etc/motd
if [ "$TMOUT" -gt 0 ]; then
    echo "Root shell: exits after $TMOUT seconds waiting for a command; foreground commands are not timed out."
else
    echo 'Root shell: automatic idle exit is disabled by the administrator.'
fi
# Ubuntu's packaged BusyBox ash does not implement TMOUT. Reuse Bash's
# built-in prompt timeout without an extra watcher, disk access or profiles.
exec /usr/bin/bash --noprofile --norc -i
