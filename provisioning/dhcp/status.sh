#!/bin/sh
# Show the current DHCP state: is dnsmasq up, active leases, recent events.
# Usage: ./status.sh   (no root needed)
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUN="$DIR/run"

pid=$(cat "$RUN/dnsmasq.pid" 2>/dev/null || true)
# dnsmasq runs as 'nobody', so kill -0 as our user hits EPERM (process exists
# but we can't signal it). Test for the live proc directory instead.
if [ -n "$pid" ] && [ -d "/proc/$pid" ]; then
	echo "dnsmasq: UP (pid $pid)"
else
	echo "dnsmasq: down"
fi

echo
echo "== active leases =="
if [ -s "$RUN/leases" ]; then
	# fields: expiry mac ip hostname clientid
	awk '{printf "  %-17s  %-15s  %s\n", $2, $3, $4}' "$RUN/leases"
else
	echo "  (none yet)"
fi

echo
echo "== recent events =="
if [ -s "$RUN/events.log" ]; then
	tail -n 10 "$RUN/events.log" | sed 's/^/  /'
else
	echo "  (none yet)"
fi
