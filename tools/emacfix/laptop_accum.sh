#!/usr/bin/env bash
# Laptop-driven leak accumulation against the phone's leakmin mserver. The laptop drives
# the RX path harder than teru, so ~3840 continuous conns reboots the phone; this keeps
# each batch at WAVES*48 conns (default 30*48=1440, proven safe) with idle between, and
# persists the cumulative LEAK count host-side after every batch so a reboot never loses
# already-collected data. mserver must already be running on the phone (PORT=22000).
#
# Usage: laptop_accum.sh <label> <batches> [waves_per_batch]
set -u
cd "$(dirname "$0")/../.." || exit 1
LABEL=$1
BATCHES=$2
WAVES=${3:-30}
IDLE=${4:-25}
PH=192.168.69.200
MC=./tools/leakmin/target/x86_64-unknown-linux-gnu/release/mclient
LOG=/tmp/laptop_accum_${LABEL}.log
: > "$LOG"

read_phone() { python3 tools/phonesh/phonesh.py "$PH" "$1" 2>/dev/null; }

# kill any stale mserver (else it keeps port 22000 and the fresh log gets unlinked), fresh log
read_phone "kill \$(ps | grep '[m]server' | awk '{print \$1}') 2>/dev/null; sleep 1; rm -f /tmp/mserver.log; ( trap '' HUP; cd /tmp && PORT=22000 ./mserver >/tmp/mserver.log 2>&1 & ); sleep 1; echo ok" >/dev/null
echo "[$LABEL] mserver (re)started; batch=$((WAVES*48)) conns x $BATCHES" | tee -a "$LOG"

for b in $(seq 1 "$BATCHES"); do
  for i in $(seq 1 "$WAVES"); do $MC ${PH}:22000 --sweep 48 --segs 64 >/dev/null 2>&1; done
  info=$(read_phone 'cat /proc/uptime; echo SEP; if [ -f /tmp/mserver.log ]; then grep -c LEAK /tmp/mserver.log; else echo NOLOG; fi')
  up=$(echo "$info" | grep -oE '^[0-9]+' | head -1)
  leaks=$(echo "$info" | sed 's/.*SEP//' | grep -oE '[0-9]+|NOLOG' | tail -1)
  echo "[$LABEL] batch=$b conns_cum=$((b*WAVES*48)) uptime=${up:-?}s leaks_cum=${leaks}" | tee -a "$LOG"
  if [ "$leaks" = NOLOG ]; then
    echo "[$LABEL] *** mserver.log gone (reboot) at batch $b — stopping; data above is preserved" | tee -a "$LOG"
    break
  fi
  [ "$b" -lt "$BATCHES" ] && sleep "$IDLE"
done
echo "[$LABEL] DONE" | tee -a "$LOG"
