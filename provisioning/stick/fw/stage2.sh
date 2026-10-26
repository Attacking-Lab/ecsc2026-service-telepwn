# ==== telepwn stage 2: take the phone over from the USB stick ====
# Sourced by stage 1 (hijack/stage1.sh) from a /tmp copy, inside phone.sh, so an update
# can replace fw/ while this runs. Stage 1 set T_ROOT (the stick's telepwn/ dir), T_LOG
# and t_log.
#
#   1. swap in the fixed emac driver and get a lease
#   2. ask the server in fw/server.txt which team this phone is -> ECSC_TEAM
#   3. if the server has a newer firmware than fw/version.txt, download it, unpack it
#      next to fw/ and swap it in, then start over with the new stage 2
#   4. launch fw/run, and never return so appstart does not run
#
# The only way back to the normal boot is a fixed emac driver that broke networking on
# the previous boot (data/.emac_try still there): then this returns 1 and stage 1 lets
# phone.sh continue. Before that reboot the stock driver is used to look for a newer
# firmware, so a broken emac_fixed.ko can be replaced by an update instead of by hand.
# A server that does not answer only costs the team and the update;
# the phone still boots the firmware already on the stick.

: "${T_STICK:=${Q_STICK}}"
: "${T_LOG:=${Q_LOG}}"

t_log() {
    echo "telepwn: $*"
    echo "$*" >>"$T_LOG"
}

T_FW=$T_ROOT/fw
T_DATA=$T_ROOT/data
T_RUN=/tmp/telepwn
T_EMAC_TRY=$T_DATA/.emac_try
T_STOCK_EMAC=/usr/driver/emac.ko

t_ip() {
    ifconfig eth0 2>/dev/null | sed -n 's/.*inet addr:\([0-9.]*\).*/\1/p'
}

# The DHCP router is the server (gateway_ip of the phone gateway); udhcpc only hands it
# to its script, so a wrapper records it before running the stock one.
t_dhcp() {
    rm -f /tmp/t_router
    cat >/tmp/t_udhcpc.sh <<'EOF'
#!/bin/sh
case "$1" in bound|renew) echo "$router" >/tmp/t_router ;; esac
exec /usr/share/udhcpc/default.script "$@"
EOF
    chmod +x /tmp/t_udhcpc.sh
    udhcpc -i eth0 -n -q -t 6 -T 2 -s /tmp/t_udhcpc.sh >/dev/null 2>&1
    [ -n "$(t_ip)" ]
}

t_emac_load() {
    ifconfig eth0 down 2>/dev/null
    rmmod emac 2>/dev/null
    insmod "$1" || return 1
    [ -n "$T_MAC" ] && ifconfig eth0 hw ether "$T_MAC" 2>/dev/null
    ifconfig eth0 up
    echo 1 >/proc/emac/eth1_down 2>/dev/null
    return 0
}

# Both drivers register as module "emac", so /tmp/emac_fixed remembers that the swap
# already happened on this boot (a re-run after an update skips it).
t_emac() {
    [ -f /tmp/emac_fixed ] && return 0
    if [ -f "$T_EMAC_TRY" ]; then
        rm -f "$T_EMAC_TRY"
        sync
        t_log "fixed emac broke networking on the last boot -> normal boot"
        return 1
    fi
    if [ ! -f "$T_FW/emac_fixed.ko" ]; then
        t_log "no emac_fixed.ko on the stick, keeping the stock driver"
        t_dhcp
        return 0
    fi
    T_MAC=$(ifconfig eth0 2>/dev/null | sed -n 's/.*HWaddr \([0-9A-Fa-f:]*\).*/\1/p')
    : >"$T_EMAC_TRY"
    sync
    if t_emac_load "$T_FW/emac_fixed.ko" && t_dhcp; then
        : >/tmp/emac_fixed
        rm -f "$T_EMAC_TRY"
        sync
        t_log "fixed emac loaded, lease $(t_ip)"
        return 0
    fi
    t_log "no lease with the fixed emac, trying the stock driver"
    if t_emac_load "$T_STOCK_EMAC" && t_dhcp; then
        t_log "stock emac gets a lease -> fixed emac is broken"
        if t_update; then
            rm -f "$T_EMAC_TRY"
            t_log "rebooting to try the emac of the new firmware"
        else
            t_log "rebooting to normal mode"
        fi
        cp "$T_LOG" "$T_DATA/boot.log" 2>/dev/null
        sync
        reboot
        sleep 120
    fi
    rm -f "$T_EMAC_TRY"
    sync
    t_log "no lease with either driver, booting the stick firmware offline"
    udhcpc -i eth0 -b -q >/dev/null 2>&1
    return 0
}

t_get() {
    [ -n "$T_SRV" ] || return 1
    "$T_FW/fetch" "http://$T_SRV$1" "$2" $3 2>>"$T_LOG"
}

t_team() {
    ECSC_TEAM=$(t_get /team - | sed 's/[^0-9A-Za-z_-]//g;q')
    ECSC_TEAM_ID=$(t_get /team_id - | sed 's/[^0-9]//g;q')
    export ECSC_TEAM ECSC_TEAM_ID
    if [ -n "$ECSC_TEAM" ]; then
        echo "$ECSC_TEAM" >/tmp/ecsc_team
        t_log "team $ECSC_TEAM (id ${ECSC_TEAM_ID:-?})"
    else
        t_log "no team from $T_SRV"
    fi
}

# Returns 0 only when a newer firmware was swapped in.
t_update() {
    _cur=$(sed 's/[^0-9]//g;q' "$T_FW/version.txt" 2>/dev/null)
    [ -n "$_cur" ] || _cur=0
    _new=$(t_get /version - | sed 's/[^0-9]//g;q')
    if [ -z "$_new" ]; then
        t_log "no version from $T_SRV, staying on $_cur"
        return 1
    fi
    [ "$_new" -gt "$_cur" ] || return 1
    t_log "updating firmware $_cur -> $_new"

    _sha=$(t_get "/fw/$_new.sha256" - | sed 's/[^0-9a-fA-F]//g;q')
    if [ "${#_sha}" -ne 64 ]; then
        t_log "bad sha256 for $_new"
        return 1
    fi
    if ! t_get "/fw/$_new.tar.gz" "$T_DATA/fw.tar.gz" "$_sha"; then
        t_log "download of $_new failed"
        return 1
    fi

    rm -rf "$T_ROOT/fw.new"
    mkdir -p "$T_ROOT/fw.new"
    if ! (cd "$T_ROOT/fw.new" && tar xzf "$T_DATA/fw.tar.gz"); then
        t_log "unpacking $_new failed"
        rm -rf "$T_ROOT/fw.new" "$T_DATA/fw.tar.gz"
        return 1
    fi
    _got=$(sed 's/[^0-9]//g;q' "$T_ROOT/fw.new/version.txt" 2>/dev/null)
    if [ "$_got" != "$_new" ] || [ ! -f "$T_ROOT/fw.new/stage2.sh" ]; then
        t_log "archive for $_new is incomplete (version '$_got')"
        rm -rf "$T_ROOT/fw.new" "$T_DATA/fw.tar.gz"
        return 1
    fi

    rm -rf "$T_ROOT/fw.old"
    mv "$T_FW" "$T_ROOT/fw.old" || return 1
    if ! mv "$T_ROOT/fw.new" "$T_FW"; then
        mv "$T_ROOT/fw.old" "$T_FW"
        return 1
    fi
    rm -rf "$T_ROOT/fw.old" "$T_DATA/fw.tar.gz"
    sync
    t_log "firmware $_new installed"
    return 0
}

# The backend opens lines/ and data/ relative to its working directory and vfat has no
# symlinks, so its working directory is a tmpfs dir linking to both halves of the stick.
t_launch() {
    export T_FW T_DATA T_RUN T_SRV
    if [ ! -f "$T_FW/run" ]; then
        t_log "no $T_FW/run"
        return
    fi
    t_log "launching $T_FW/run (version $(cat "$T_FW/version.txt" 2>/dev/null))"
    sh "$T_FW/run" >/tmp/telepwn.log 2>&1 &
}

t_stage2() {
    mkdir -p "$T_DATA"
    t_emac || return 1
    ifconfig eth0 mtu 1420
    _srv=$(sed 's/[^0-9A-Za-z.:-]//g;q' "$T_FW/server.txt" 2>/dev/null)
    _myip=$(ifconfig eth0 2>/dev/null | sed -n 's/.*inet addr:\([0-9.]*\).*/\1/p')
    _pfx=$(echo "$_myip" | cut -d. -f1-3)
    _port=$(echo "$_srv" | sed -n 's/.*:\([0-9]*\)$/\1/p')
    [ -n "$_port" ] || _port=9003
    _gw=$(sed 's/ .*//;q' /tmp/t_router 2>/dev/null)
    [ -n "$_gw" ] || _gw=$(route -n 2>/dev/null | awk '$1=="0.0.0.0"{print $2; exit}')
    if [ -n "$_gw" ]; then
        route add -host "$_gw" dev eth0 2>/dev/null
        T_SRV="$_gw:$_port"
    elif [ -n "$_pfx" ]; then
        T_SRV="$_pfx.1:$_port"
    else
        T_SRV="$_srv"
    fi
    t_log "server $T_SRV (ip $_myip)"
    t_team
    if t_update; then
        cp "$T_FW/stage2.sh" "/tmp/stage2.$_new.sh"
        . "/tmp/stage2.$_new.sh"
        return $?
    fi
    t_launch
    cp "$T_LOG" "$T_DATA/boot.log" 2>/dev/null
    sync
    while :; do
        sleep 3600
    done
}

t_stage2
# ==== end telepwn stage 2 ====
