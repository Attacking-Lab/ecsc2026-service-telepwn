#!/usr/bin/env python3
"""Re-hijack stock/reverted phones on their live team VLANs into cool mode.

Unlike orchestrate.py (bench DHCP, factory .84 on a single NIC), this drives phones
that are already live on the switch in their team VLANs: it resolves MAC->IP from the
running vlan dnsmasq lease file and reuses orchestrate's web_flash / telnet_patch /
probe_state state machine. Phones already at .85 skip the .84->.85 web-flash.

Per phone the reverse-nc push source and the baked fallback server are the phone's own
VLAN gateway (10.42.<vlan>.1); divert.sh self-derives the real NFS server from the
phone's DHCP address anyway.

Usage:  rehijack.py --batch 5 <mac>:<vlan> [<mac>:<vlan> ...]
"""
import argparse
import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROV = os.path.dirname(HERE)
sys.path.insert(0, PROV)
sys.path.insert(0, os.path.join(PROV, "webflash"))
sys.path.insert(0, HERE)

import orchestrate as O  # noqa: E402

LEASES = os.path.join(PROV, "vlans", "run", "leases")


def live_leases():
    out = []
    try:
        with open(LEASES) as f:
            for line in f:
                p = line.split()
                if len(p) >= 3:
                    out.append((p[1].lower(), p[2]))
    except FileNotFoundError:
        pass
    return out


O.leases = live_leases

O.ROM_TELNET1 = os.path.join(os.path.dirname(PROV), "telnet1.rom")
O.ROM_85 = os.path.join(os.path.dirname(PROV), "T46S-66.85.0.5.rom")

import yealink_web  # noqa: E402


def _web_flash_https(ip, rom, label):
    """orchestrate.web_flash, but forcing https: factory firmware hangs the bare
    `GET http://host/` that scheme='auto' uses to detect the redirect."""
    O.log(f"[{ip}] web-flash {label}")
    last = None
    for attempt in range(3):
        try:
            w = yealink_web.YealinkWeb(ip, scheme="https", timeout=45, verbose=False)
            w.login()
            break
        except Exception as e:  # noqa: BLE001
            last = e
            O.log(f"[{ip}] login attempt {attempt + 1} failed: {e}")
            time.sleep(5)
    else:
        raise last
    w.upload_firmware(rom, poll=True, poll_timeout=150)
    O.log(f"[{ip}] {label} uploaded; phone rebooting to apply")


O.web_flash = _web_flash_https

import deploy85  # noqa: E402
from ptelnet import Phone  # noqa: E402

FLASH_LOCK = threading.Lock()
PATCH_LOCK = threading.Lock()


def gw(vlan):
    return f"10.42.{vlan}.1"


def tcp_open(ip, port, timeout=3):
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def resolve_ip(mac):
    for m, ip in live_leases():
        if m == mac:
            return ip
    return None


def wait_port(ip, port, want_open, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if tcp_open(ip, port) == want_open:
            return True
        time.sleep(5)
    return False


def reboot_phone(ip):
    try:
        with Phone(ip) as p:
            p.s.sendall(b"reboot\n")
            time.sleep(2)
    except Exception:
        pass


def rehijack_one(mac, vlan, results):
    g = gw(vlan)
    ip = resolve_ip(mac)
    if not ip:
        results[mac] = "NO_LEASE"
        O.log(f"[{mac}] no DHCP lease; skipping")
        return
    if tcp_open(ip, 23):
        results[mac] = "already-cool"
        O.log(f"[{mac}@{ip}] telnet already open; already cool")
        return

    window = False
    for flash_try in range(1, 4):
        with FLASH_LOCK:
            O.log(f"[{mac}@{ip}] flashing telnet1 (attempt {flash_try}/3)")
            try:
                _web_flash_https(ip, O.ROM_TELNET1, "telnet1 (update-stage)")
            except Exception as e:  # noqa: BLE001
                O.log(f"[{mac}@{ip}] flash attempt {flash_try} error: {e}")
                continue
        ip = resolve_ip(mac) or ip
        O.log(f"[{mac}@{ip}] waiting for telnet update-stage window (try {flash_try})...")
        if wait_port(ip, 23, True, 420):
            window = True
            break
        O.log(f"[{mac}@{ip}] no window after attempt {flash_try}; re-flashing")
    if not window:
        results[mac] = "no-telnet-window"
        O.log(f"[{mac}@{ip}] telnet window never opened after 3 flashes")
        return

    ip = resolve_ip(mac) or ip
    with PATCH_LOCK:
        O.log(f"[{mac}@{ip}] patching divert hook")
        try:
            deploy85.deploy(ip, g, g, False, "divert.sh")
        except Exception as e:  # noqa: BLE001
            results[mac] = f"patch-error: {e}"
            O.log(f"[{mac}@{ip}] patch ERROR: {e}")
            return

    O.log(f"[{mac}@{ip}] rebooting into cool mode")
    reboot_phone(ip)
    time.sleep(20)
    ip = resolve_ip(mac) or ip
    if wait_port(ip, 23, True, 300):
        results[mac] = "coolmode"
        O.log(f"[{mac}@{ip}] COOL MODE")
    else:
        results[mac] = "no-cool"
        O.log(f"[{mac}@{ip}] did not reach cool mode")


def worker(mac, vlan, results):
    try:
        rehijack_one(mac, vlan, results)
    except Exception as e:  # noqa: BLE001
        O.log(f"[{mac}] ERROR: {e}")
        results[mac] = f"ERROR: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="+", help="mac:vlan pairs")
    ap.add_argument("--batch", type=int, default=5)
    args = ap.parse_args()

    pairs = []
    for t in args.targets:
        mac, vlan = t.rsplit(":", 1)
        pairs.append((mac.lower(), int(vlan)))

    results = {}
    for i in range(0, len(pairs), args.batch):
        chunk = pairs[i:i + args.batch]
        O.log(f"=== batch {i // args.batch + 1}: {[m for m, _ in chunk]} ===")
        threads = []
        for mac, vlan in chunk:
            th = threading.Thread(target=worker, args=(mac, vlan, results), daemon=True)
            th.start()
            threads.append(th)
            time.sleep(1)
        for th in threads:
            th.join()
        O.log(f"=== batch {i // args.batch + 1} done ===")

    O.log("=== SUMMARY ===")
    for mac, vlan in pairs:
        O.log(f"  {mac} vlan{vlan}: {results.get(mac, '??')}")
    bad = [m for m, r in results.items() if r != "coolmode"]
    O.log(f"cool: {len(results) - len(bad)}/{len(pairs)}  failed: {bad}")


if __name__ == "__main__":
    main()
