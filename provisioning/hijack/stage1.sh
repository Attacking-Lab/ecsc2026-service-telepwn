# ==== telepwn stage 1: boot from the USB stick ====
# Installed as /phone/telepwn/divert.sh and sourced by phone.sh right before the
# `appstart` line (deploy85.py --divert stage1.sh). telnetd was restarted just before.
#
#   stick with telepwn/fw/stage2.sh -> source a /tmp copy of it. Stage 2 takes the phone
#                                       over and never returns, except to hand the phone
#                                       back to the normal boot (see stage2.sh).
#   no stick, or no stage 2 on it    -> return, so phone.sh reaches appstart and the
#                                       phone boots its own software.
#
# Everything else -- the emac driver, the server address, the firmware -- lives on the
# stick, so this file is the only telepwn artifact on flash and never needs to change.

T_STICK=/tmp/udisk1
T_ROOT=$T_STICK/telepwn
T_LOG=/tmp/telepwn-boot.log
T_MOUNTED=""

t_log() {
    echo "telepwn: $*"
    echo "$*" >>"$T_LOG"
}

# udev normally provides /dev/sdXN; fall back to a node of our own from /sys so a slow
# udev cannot send a phone with a stick into the normal boot.
t_blockdev() {
    _name=$1
    if [ -b "/dev/$_name" ]; then
        echo "/dev/$_name"
        return 0
    fi
    for _s in /sys/block/*/"$_name"/dev /sys/block/"$_name"/dev; do
        [ -r "$_s" ] || continue
        _mm=$(cat "$_s")
        mknod "/tmp/t_$_name" b "${_mm%%:*}" "${_mm##*:}" 2>/dev/null
        [ -b "/tmp/t_$_name" ] && echo "/tmp/t_$_name" && return 0
    done
    return 1
}

t_mount_stick() {
    grep -q " $T_STICK " /proc/mounts && return 0
    mkdir -p "$T_STICK"
    _i=0
    while [ "$_i" -lt 45 ]; do
        for _n in sda1 sdb1 sdc1 sdd1 sda sdb sdc sdd; do
            _dev=$(t_blockdev "$_n") || continue
            if mount -t ext2 "$_dev" "$T_STICK" >/dev/null 2>&1 ||
                mount -t vfat -o quiet "$_dev" "$T_STICK" >/dev/null 2>&1 ||
                mount -t vfat "$_dev" "$T_STICK" >/dev/null 2>&1; then
                if [ -f "$T_ROOT/fw/stage2.sh" ]; then
                    T_MOUNTED=$_dev
                    t_log "stick $_dev mounted on $T_STICK"
                    return 0
                fi
                umount "$T_STICK" >/dev/null 2>&1
            fi
        done
        _i=$(expr "$_i" + 1)
        sleep 1
    done
    return 1
}

t_stage1() {
    if ! t_mount_stick; then
        t_log "no stick -> normal boot"
        return 1
    fi
    if [ ! -f "$T_ROOT/fw/stage2.sh" ]; then
        t_log "no $T_ROOT/fw/stage2.sh -> normal boot"
        return 1
    fi
    cp "$T_ROOT/fw/stage2.sh" /tmp/stage2.sh || return 1
    . /tmp/stage2.sh
}

t_stage1
[ -n "$T_MOUNTED" ] && umount "$T_STICK" >/dev/null 2>&1
return 0 2>/dev/null || exit 0
# ==== end telepwn stage 1 ====
