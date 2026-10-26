#!/bin/sh
# Bring up the provisioning DHCP server on a single interface.
# Usage: sudo ./up.sh [IFACE] [SERVER_IP]
#   IFACE      p2p/bench NIC to serve on   (default enp1s0f0)
#   SERVER_IP  address given to this host  (default 10.42.0.1, /24)
set -eu

IFACE="${1:-enp1s0f0}"
SRV_IP="${2:-10.42.0.1}"
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUN="$DIR/run"
mkdir -p "$RUN"

echo "[*] assigning ${SRV_IP}/24 to ${IFACE}"
ip addr flush dev "$IFACE" 2>/dev/null || true
ip addr add "${SRV_IP}/24" dev "$IFACE"
ip link set "$IFACE" up

# stop a previous instance if one is lingering
if [ -f "$RUN/dnsmasq.pid" ]; then
	kill "$(cat "$RUN/dnsmasq.pid")" 2>/dev/null || true
fi

echo "[*] starting dnsmasq on ${IFACE}"
dnsmasq \
	--conf-file="$DIR/dnsmasq.conf" \
	--interface="$IFACE" \
	--dhcp-leasefile="$RUN/leases" \
	--dhcp-script="$DIR/lease-hook.sh" \
	--log-facility="$RUN/dnsmasq.log" \
	--pid-file="$RUN/dnsmasq.pid"

echo "[+] up. iface=${IFACE} server=${SRV_IP}"
echo "    leases: $RUN/leases"
echo "    events: $RUN/events.log"
echo "    log:    $RUN/dnsmasq.log"
