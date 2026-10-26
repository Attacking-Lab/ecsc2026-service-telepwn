#!/bin/sh
# dnsmasq dhcp-script hook. Fires on every lease add/old/del.
# Args: $1=action(add|old|del)  $2=mac  $3=ip  $4=hostname
# This is the orchestrator's entry point: "MAC=x is now at IP=y".
# For now it just records events; the state machine will hang off this later.
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
RUN="$DIR/run"
mkdir -p "$RUN"

ts=$(date -Is 2>/dev/null || date)
printf '%s\taction=%s\tmac=%s\tip=%s\thost=%s\n' \
	"$ts" "${1:-}" "${2:-}" "${3:-}" "${4:-}" >> "$RUN/events.log"

exit 0
