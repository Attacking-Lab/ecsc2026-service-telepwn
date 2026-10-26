#!/usr/bin/env python3
"""Re-stage the current divert.sh onto every phone already in cool mode.

Pushes the current divert.sh and removes any obsolete flash artifacts on each phone,
without reflashing and without a reboot: the new bootstrap takes effect on the phone's
next boot, so the running cool mode is undisturbed.

Targets are discovered from the callback log (latest report per team VLAN gives each
phone's current IP); the server/host address is that subnet's .1 gateway. Safe to
re-run: deploy85 --stage-only is idempotent.
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROV = os.path.dirname(HERE)
LOG = os.path.join(PROV, "callbacks.log")


def latest_targets():
    """[(vlan, ip, gw)] — most recent phone IP per team VLAN from the callback log."""
    latest = {}  # vlan -> ip
    with open(LOG) as f:
        for line in f:
            if "MATCH" not in line:
                continue
            m = re.search(r"peer=(\S+)", line)
            if not m:
                continue
            octs = m.group(1).split(".")
            if len(octs) != 4 or not octs[2].isdigit():
                continue
            vlan = int(octs[2])
            if vlan in (0, 1):  # skip staging network
                continue
            latest[vlan] = m.group(1)
    out = [(vlan, ip, ".".join(ip.split(".")[:3]) + ".1")
           for vlan, ip in latest.items()]
    out.sort()
    return out


def main():
    only = set(int(a) for a in sys.argv[1:])  # optional vlan filter
    targets = latest_targets()
    if only:
        targets = [t for t in targets if t[0] in only]
    print(f"[restage] {len(targets)} phone(s)")
    ok = fail = 0
    for vlan, ip, gw in targets:
        r = subprocess.run(
            ["python3", os.path.join(HERE, "deploy85.py"),
             ip, "--server", gw, "--host-ip", gw, "--stage-only"],
            capture_output=True, text=True, timeout=120,
        )
        good = "divert.sh syntax: OK" in r.stdout
        chk = subprocess.run(
            ["python3", os.path.join(HERE, "ptelnet.py"), ip, "-c",
             "grep -c beacon /phone/telepwn/divert.sh; ls /phone/telepwn"],
            capture_output=True, text=True, timeout=60,
        )
        first = chk.stdout.strip().splitlines()[:1]
        has_beacon = bool(first) and first[0].strip() not in ("0", "")
        clean = "run.sh" not in chk.stdout
        status = "ok" if (good and has_beacon and clean) else "FAIL"
        ok, fail = (ok + 1, fail) if status == "ok" else (ok, fail + 1)
        print(f"[restage]   vlan {vlan:<4} {ip:<15} {status}"
              + ("" if status == "ok" else f"  ({r.stdout.strip().splitlines()[-1:]})"))
    print(f"[restage] {ok} ok, {fail} fail")


if __name__ == "__main__":
    main()
