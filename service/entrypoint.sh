#!/bin/sh
# Run with host networking: the backend reaches the frontend over the kmob0
# interface, which is addressed 10.60.X.2 where X is the team id. Wait for that
# address, then derive and export the team id (X) and the frontend address
# (10.60.X.4) so the backend can read them from the environment. If kmob0 is
# absent (plain x86 deployments) start immediately with the vars unset.
set -eu

iface=kmob0
if [ -e "/sys/class/net/$iface" ]; then
    echo "entrypoint: waiting for $iface to get an IPv4 address..."
    while ! ip -4 addr show dev "$iface" 2>/dev/null | grep -q 'inet '; do
        sleep 1
    done

    addr=$(ip -4 -o addr show dev "$iface" | awk '{print $4}' | head -n1)
    ip=${addr%/*}
    team_id=$(echo "$ip" | cut -d. -f3)

    export TEAM_ID="$team_id"
    export TELEPWN_IP="10.60.$team_id.4"
    echo "entrypoint: $iface up: $ip -> TEAM_ID=$TEAM_ID TELEPWN_IP=$TELEPWN_IP"
fi

# Reap the item store in the background: once disk usage passes 80%, delete
# item_* dirs older than the 600s TTL, then prune any parent dirs that became
# empty (never the items root). The df gate is O(1) (statvfs, no tree walk).
# Loops forever on a 60s tick.
items=/tmp/telepwn_data/backend/items
(
    while :; do
        usage=$(df -P /tmp/telepwn_data 2>/dev/null | awk 'NR==2 {print $5+0}')
        if [ "${usage:-0}" -ge 80 ]; then
            find "$items" -type d -name 'item_*' ! -newermt '-600 seconds' -prune -exec rm -rf {} + 2>/dev/null
            find "$items" -mindepth 1 -depth -type d -empty -delete 2>/dev/null
        fi
        sleep 60
    done
) &

exec "$@"
