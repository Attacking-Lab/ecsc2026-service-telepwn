#!/usr/bin/env python3
"""Restart the share launcher on every phone already in cool mode, in place.

Use after changing the launcher (content/common/run) and rebuilding the shares: the
phones run a copy of the launcher in memory, so a share change is only picked up on the
next boot. This kills the running launcher (and its lcdfb/keypad display) and starts the
new one from the mount, with no reboot -- the running backend is adopted by the new
launcher, the key check comes up on screen and the heartbeat resumes at once.

Targets are discovered from the callback log (latest report per VLAN gives each phone's
current IP); the server/gateway is that subnet's .1. Safe to re-run.
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROV = os.path.dirname(HERE)
LOG = os.path.join(PROV, "callbacks.log")

RELAUNCH = (
    'for p in telepwn/run telepwn/lcdfb telepwn/keypad; do '
    'ps | grep $p | grep -v grep | while read pid rest; do '
    'kill $pid 2>/dev/null; done; done; sleep 1; '
    '(trap "" HUP; /tmp/telepwn/run {srv} /tmp/telepwn >/tmp/telepwn.log 2>&1 &); '
    'sleep 2; ps | grep telepwn/keypad | grep -v grep | grep -c /tmp/telepwn/keypad'
)


def latest_targets():
    """[(vlan, ip, gw)] — most recent phone IP per team VLAN from the callback log."""
    latest = {}  # vlan -> ip
    with open(LOG) as f:
        for line in f:
            m = re.search(r"peer=(\S+).*\bvlan=(\d+)\b", line)
            if not m:
                m2 = re.search(r"peer=(\S+)", line)
                if not m2:
                    continue
                ip = m2.group(1)
                octs = ip.split(".")
                if len(octs) != 4 or not octs[2].isdigit():
                    continue
                latest[int(octs[2])] = ip
                continue
            latest[int(m.group(2))] = m.group(1)
    out = []
    for vlan, ip in latest.items():
        if vlan in (0, 1):  # skip staging network
            continue
        gw = ".".join(ip.split(".")[:3]) + ".1"
        out.append((vlan, ip, gw))
    out.sort()
    return out


def main():
    only = set(int(a) for a in sys.argv[1:])  # optional vlan filter
    targets = latest_targets()
    if only:
        targets = [t for t in targets if t[0] in only]
    print(f"[relaunch] {len(targets)} phone(s)")
    ok = fail = 0
    for vlan, ip, gw in targets:
        cmd = RELAUNCH.format(srv=gw)
        r = subprocess.run(
            ["python3", os.path.join(HERE, "ptelnet.py"), ip, "-c", cmd],
            capture_output=True, text=True, timeout=90,
        )
        count = r.stdout.strip().splitlines()[-1:] or [""]
        good = count[0].strip() == "1"
        ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
        print(f"[relaunch]   vlan {vlan:<4} {ip:<15} {'ok' if good else 'FAIL'}")
    print(f"[relaunch] {ok} ok, {fail} fail")


if __name__ == "__main__":
    main()
