#!/bin/sh
# Mount watchdog for the telepwn USB stick. The run launcher kills the phone's native
# hot-plug daemon (hpdserver), so nothing (re)mounts /tmp/udisk1 if the stick drops off
# the bus or is re-inserted -- and with no udev/devtmpfs/hotplug helper on this 2.6.27
# kernel the stick can come back as sdb/sdc with no /dev node at all. So every few
# seconds: if the current mount went stale (its backing device vanished from
# /proc/partitions) drop it, then if /tmp/udisk1 is not mounted, find a stick partition in
# /proc/partitions, mknod its node if missing, and (re)mount it. On a successful
# (re)mount, kill frontend+backend so their run supervisors restart them against the
# fresh inodes (they held dead inodes from the old mount). busybox 1.10.3: no `grep -E`,
# no `grep -w`.
MNT=/tmp/udisk1
POLL=3

backing_dev() {
    awk -v m="$MNT" '$2==m {print $1; exit}' /proc/mounts
}

dev_present() {
    awk -v n="$1" '$4==n {f=1} END {exit !f}' /proc/partitions
}

drop_stale() {
    dev=$(backing_dev)
    [ -n "$dev" ] || return 0
    dev_present "${dev##*/}" || umount -l "$MNT" 2>/dev/null
}

stick_names() {
    awk '$4 ~ /^sd[a-z][0-9]/ {print $4}' /proc/partitions
    awk '$4 ~ /^sd[a-z]$/ {print $4}' /proc/partitions
}

remount() {
    mkdir -p "$MNT"
    for name in $(stick_names); do
        dev=/dev/$name
        if [ ! -b "$dev" ]; then
            mm=$(awk -v n="$name" '$4==n {print $1" "$2; exit}' /proc/partitions)
            [ -n "$mm" ] && mknod "$dev" b "${mm% *}" "${mm#* }" 2>/dev/null
        fi
        [ -b "$dev" ] || continue
        if mount -t ext2 -o rw,errors=continue "$dev" "$MNT" 2>/dev/null; then
            # killall frontend 2>/dev/null
            sleep 5
            killall backend 2>/dev/null
            return 0
        fi
    done
    return 1
}

while :; do
    drop_stale
    grep -q " $MNT " /proc/mounts || remount
    sleep "$POLL"
done
