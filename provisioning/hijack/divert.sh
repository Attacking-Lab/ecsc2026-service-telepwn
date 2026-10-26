# ==== telepwn provisioning diversion ====
# Sourced by phone.sh right before the `appstart` line (via a single guarded line, so a
# missing or broken file just falls through to the normal boot). telnetd was (re)started
# just before this hook, after TelnetServices.sh's `killall -9 telnetd`, so it survives.
#
# eth0 is already up (rcS loaded emac.ko + `ifconfig eth0 up`) but has no IP yet. Once
# Linux is up we take over *here*, before the phone application launches. The switch is
# the provisioning server's beacon -- a tiny TCP ack service (this subnet's .1, the VLAN
# gateway):
#
#   beacon answers -> take over: run this phone's local payload (the USB stick's
#                     telepwn/run), appstart/userland suppressed. If the stick is not
#                     ready yet we hold in idle takeover (telnet up) and retry; once the
#                     server is known present we never fall back to the phone software.
#   beacon silent  -> provisioning server absent (or down). Return, so phone.sh reaches
#                     `appstart` and the phone boots its own normal software.
#
# The beacon is on-link, so a missing server is detected fast on ARP even though this
# busybox nc has no -w. Everything that changes -- the launcher, the display binary and
# the flag -- lives on the stick; this file is the only telepwn artifact on flash.

PROV_SRV=__PROV_SRV__
BEACON_PORT=9002
MNT=/tmp/telepwn
DIR=/phone/telepwn
MYIP=""
SRV=""
PROV_MNT=""

# Locate this phone's local payload (the USB stick) and set PROV_MNT. Returns 1 if none.
prov_share() {
    for _d in /tmp/udisk1/telepwn /mnt/udisk1/telepwn; do
        if [ -x "$_d/run" ]; then
            PROV_MNT=$_d
            echo "telepwn: using local payload at $_d"
            return 0
        fi
    done
    echo "telepwn: no local payload"
    return 1
}

# Bring eth0 up with a lease. Only needs to succeed once; the retry loop below does not
# re-DHCP unless we still have no address, so a live lease is never disturbed.
prov_dhcp() {
    [ -n "$MYIP" ] && return 0
    # busybox udhcpc applies the lease via /usr/share/udhcpc/default.script.
    udhcpc -i eth0 -n -q -t 4 -T 2 >/dev/null 2>&1 || return 1
    MYIP=$(ifconfig eth0 2>/dev/null | sed -n 's/.*inet addr:\([0-9.]*\).*/\1/p')
    [ -n "$MYIP" ]
}

# The provisioning server is this subnet's .1 (the VLAN gateway); the address baked into
# the diversion is only a fallback for the flat bench network with no per-team VLAN.
prov_srv() {
    _pfx=$(echo "$MYIP" | cut -d. -f1-3)
    if [ -n "$_pfx" ]; then
        SRV="$_pfx.1"
    else
        SRV="$PROV_SRV"
    fi
    [ -n "$SRV" ]
}

# Is the provisioning server there? Its beacon replies with a fixed ack line on connect.
# The server is on-link, so a missing one fails fast on ARP even though this busybox nc
# has no -w. Requiring the ack text avoids a false positive from some other open port.
prov_beacon() {
    [ -n "$SRV" ] || return 1
    busybox nc "$SRV" "$BEACON_PORT" </dev/null 2>/dev/null | grep -q TELEPWN
}

# One attempt to find this phone's local payload and hand off to its launcher. The
# launcher is run straight from the stick -- nothing executes from flash. Sets PROV_MNT.
prov_mount() {
    prov_share || return 1
    if [ ! -x "$PROV_MNT/run" ]; then
        echo "telepwn: payload has no executable run"
        PROV_MNT=""
        return 1
    fi
    "$PROV_MNT/run" "$SRV" "$PROV_MNT" >/tmp/telepwn.log 2>&1 &
    return 0
}

# ---- boot-mode decision --------------------------------------------------------------
# The provisioning server is on-link and answers a beacon; if it stays silent this is not
# our network and phone.sh should boot the phone normally. But the switch access port can
# take ~15-30s to begin forwarding after link-up (and a boot out of the telnet1 update
# stage brings the network up late), so a single early probe races the boot and bails to
# the stock phone by mistake. Retry DHCP + beacon over a bounded window first: a lease
# plus a beacon ends it immediately, and only a server that is still silent after the
# whole window counts as absent.
PROV_UP=""
for _try in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if prov_dhcp; then
        prov_srv
        if prov_beacon; then
            PROV_UP=1
            break
        fi
    fi
    sleep 3
done
if [ -z "$PROV_UP" ]; then
    echo "telepwn: no beacon after retries -> provisioning server absent -> normal boot"
    return 0 2>/dev/null || exit 0
fi

# Server present: take over. From here appstart never runs; telnetd is already up. If the
# stick payload is not ready yet we hold in idle takeover and retry, but we do not hand
# the phone back to its own software once the server is known present.
echo "telepwn: beacon ok on $SRV -> takeover"
if prov_mount; then
    echo "telepwn: payload running -> cool mode; appstart suppressed"
else
    echo "telepwn: server present but payload not ready -> idle takeover (telnet up); will retry"
fi
while : ; do
    if [ -z "$PROV_MNT" ]; then
        # A phone that booted before its stick was ready self-heals into cool mode.
        prov_mount && echo "telepwn: payload appeared -> cool mode"
    fi
    sleep 30
done
# ==== end telepwn diversion ====
