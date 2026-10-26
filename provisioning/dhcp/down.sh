#!/bin/sh
# Tear down the provisioning DHCP server.
# Usage: sudo ./down.sh [IFACE]
set -u

IFACE="${1:-enp1s0f0}"
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUN="$DIR/run"

if [ -f "$RUN/dnsmasq.pid" ]; then
	kill "$(cat "$RUN/dnsmasq.pid")" 2>/dev/null || true
	rm -f "$RUN/dnsmasq.pid"
	echo "[*] dnsmasq stopped"
else
	pkill -f "dnsmasq.*$RUN" 2>/dev/null || true
fi

ip addr flush dev "$IFACE" 2>/dev/null || true
echo "[+] down. ${IFACE} address flushed"
