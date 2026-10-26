#!/usr/bin/env bash
# Reboot-safe leak accumulation: run N short 80-wave sessions (3840 conns each, the
# proven-safe burst size) against the phone's leakmin mserver, reading the cumulative
# LEAK count after each session and persisting it host-side so a watchdog reboot can
# never lose already-collected data. 20s idle between sessions lets the phone drain.
#
# Usage: accum_test.sh <label> <sessions>
set -u
cd "$(dirname "$0")/../.." || exit 1
LABEL=$1
SESSIONS=$2
PH=192.168.69.200
LOG=/tmp/accum_${LABEL}.log
: > "$LOG"

read_phone() { python3 tools/phonesh/phonesh.py "$PH" "$1" 2>/dev/null | tail -3; }

# fresh mserver
read_phone "rm -f /tmp/mserver.log; ( trap '' HUP; cd /tmp && PORT=22000 ./mserver >/tmp/mserver.log 2>&1 & ); sleep 1; echo started" >/dev/null
echo "[$LABEL] mserver launched" | tee -a "$LOG"

for s in $(seq 1 "$SESSIONS"); do
  ssh teru "bash -c 'for i in \$(seq 1 80); do /tmp/mclient ${PH}:22000 --sweep 48 --segs 64 >/dev/null 2>&1; done'" >/dev/null 2>&1
  info=$(read_phone 'cat /proc/uptime; echo SEP; if [ -f /tmp/mserver.log ]; then grep -c LEAK /tmp/mserver.log; else echo NOLOG; fi')
  up=$(echo "$info" | grep -oE '^[0-9]+' | head -1)
  leaks=$(echo "$info" | sed 's/.*SEP//' | grep -oE '[0-9]+|NOLOG' | tail -1)
  echo "[$LABEL] session=$s conns_cum=$((s*3840)) uptime=${up:-?}s leaks_cum=${leaks}" | tee -a "$LOG"
  if [ "$leaks" = NOLOG ]; then
    echo "[$LABEL] *** mserver.log gone (reboot) at session $s — stopping, data above preserved" | tee -a "$LOG"
    break
  fi
  sleep 20
done
echo "[$LABEL] DONE" | tee -a "$LOG"
