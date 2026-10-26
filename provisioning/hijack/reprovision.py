import os
import sys
import time
import socket

HERE = os.path.dirname(os.path.abspath(__file__))
PROV = os.path.dirname(HERE)
sys.path.insert(0, PROV)
sys.path.insert(0, os.path.join(PROV, "webflash"))
sys.path.insert(0, HERE)

import orchestrate as O
O.ROM_TELNET1 = os.path.join(os.path.dirname(PROV), "telnet1.rom")
import yealink_web
import deploy85
from ptelnet import Phone

TARGETS = [
    ("ukraine", "10.42.134.69", 134),
    ("latvia", "10.42.119.215", 119),
]


def log(m):
    print(time.strftime("%H:%M:%S"), m, flush=True)


def o(ip, p, t=3):
    s = socket.socket()
    s.settimeout(t)
    try:
        s.connect((ip, p))
        s.close()
        return True
    except Exception:
        return False


def flash_telnet1(ip):
    if o(ip, 23):
        return True
    for a in range(3):
        try:
            w = yealink_web.YealinkWeb(ip, scheme="https", timeout=45, verbose=False)
            w.login()
            w.upload_firmware(O.ROM_TELNET1, poll=True, poll_timeout=150)
            break
        except Exception as e:
            log("  flash try %d err: %s" % (a + 1, str(e)[:60]))
            time.sleep(5)
    t0 = time.time()
    while time.time() - t0 < 420:
        if o(ip, 23):
            return True
        time.sleep(5)
    return False


def has_stick(ip):
    with Phone(ip) as p:
        out = p.run("cat /proc/partitions 2>/dev/null | grep sd | head -n 1; echo X")
    return bool(out.split("X")[0].strip())


def provision(name, ip, vlan):
    gw = "10.42.%d.1" % vlan
    log("[%s@%s] flashing telnet1 for shell window" % (name, ip))
    if not flash_telnet1(ip):
        return (name, "no-telnet-window")
    stick = has_stick(ip)
    hook = "stage1.sh" if stick else "divert.sh"
    log("[%s@%s] stick=%s -> hook=%s; full deploy85 (patch phone.sh)" % (name, ip, stick, hook))
    try:
        deploy85.deploy(ip, gw, gw, False, hook)
    except Exception as e:
        return (name, "deploy-err:%s" % str(e)[:40])
    with Phone(ip) as p:
        p.s.sendall(b"reboot\n")
        time.sleep(3)
    log("[%s@%s] rebooted; waiting for cool mode" % (name, ip))
    t0 = time.time()
    while time.time() - t0 < 360:
        if o(ip, 23):
            break
        time.sleep(6)
    if not o(ip, 23):
        return (name, "no-cool-timeout")
    time.sleep(5)
    try:
        if stick:
            v, tm = "", ""
            t1 = time.time()
            while time.time() - t1 < 150:
                try:
                    with Phone(ip) as p:
                        out = p.run("cat /tmp/udisk1/telepwn/fw/version.txt 2>/dev/null; echo V; "
                                    "cat /tmp/ecsc_team 2>/dev/null; echo T", wait=12)
                    out = out.replace("\r", "")
                    v = out.split("V")[0].strip()
                    tm = out.split("V")[1].split("T")[0].strip() if "V" in out else ""
                    if v == "3" and tm:
                        break
                except Exception:
                    pass
                time.sleep(8)
            return (name, "cool stick v%s team=%s" % (v, tm))
        with Phone(ip) as p:
            if False:
                pass
            else:
                out = p.run("mount | grep nets; echo M; cat /tmp/telepwn/team.txt 2>/dev/null; echo T",
                            wait=12)
                out = out.replace("\r", "")
                mounted = "nets" in out
                tm = out.split("M")[1].split("T")[0].strip() if "M" in out else ""
                return (name, "cool nfs mounted=%s team=%s" % (mounted, tm))
    except Exception as e:
        return (name, "verify-err:%s" % str(e)[:40])


def main():
    only = sys.argv[1:] if len(sys.argv) > 1 else None
    results = {}
    for name, ip, vlan in TARGETS:
        if only and name not in only:
            continue
        log("=== provisioning %s ===" % name)
        try:
            r = provision(name, ip, vlan)
        except Exception as e:
            r = (name, "ERROR:%s" % str(e)[:50])
        results[r[0]] = r[1]
        log("   %s -> %s" % (r[0], r[1]))
    log("=== SUMMARY ===")
    for n, r in results.items():
        log("   %-10s %s" % (n, r))


if __name__ == "__main__":
    main()
