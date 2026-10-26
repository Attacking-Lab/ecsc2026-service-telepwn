import sys
import time
import socket
import threading
import concurrent.futures as cf

sys.path.insert(0, "/opt/telepwn/provisioning")
sys.path.insert(0, "/opt/telepwn/provisioning/hijack")
import deploy85
from ptelnet import Phone

TARGETS = [
    ("belgium", 104), ("bulgaria", 105), ("croatia", 106), ("cyprus", 107),
    ("czechia", 108), ("denmark", 109), ("estonia", 110), ("finland", 111),
    ("france", 112), ("germany", 113), ("greece", 114), ("hungary", 115),
    ("iceland", 116), ("ireland", 117), ("italy", 118), ("luxembourg", 122),
    ("norway", 124), ("poland", 125), ("portugal", 126), ("serbia", 128),
    ("slovakia", 129), ("spain", 131), ("sweden", 132), ("switzerland", 133),
    ("united-kingdom", 135), ("australia", 136), ("canada", 137), ("kosovo", 139),
    ("usa", 140), ("armenia", 141), ("malta", 142), ("singapore", 143),
]
WAVE = 6
DEPLOY_LOCK = threading.Lock()


def topen(ip, port, t=1.2):
    s = socket.socket()
    s.settimeout(t)
    try:
        s.connect((ip, port))
        s.close()
        return True
    except Exception:
        return False


def discover(vlan):
    base = "10.42.%d." % vlan
    cands = ["%s%d" % (base, h) for h in range(50, 241)]
    with cf.ThreadPoolExecutor(max_workers=64) as ex:
        hits = [ip for ip, ok in zip(cands, ex.map(lambda ip: topen(ip, 23), cands)) if ok]
    return hits[0] if hits else None


def log(m):
    print(time.strftime("%H:%M:%S"), m, flush=True)


def ver_team(ip):
    try:
        with Phone(ip) as p:
            out = p.run("cat /tmp/udisk1/telepwn/fw/version.txt 2>/dev/null; echo V; "
                        "cat /tmp/ecsc_team 2>/dev/null; echo T", wait=10)
        out = out.replace("\r", "")
        v = out.split("V")[0].strip()
        t = out.split("V")[1].split("T")[0].strip() if "V" in out else ""
        return v, t
    except Exception:
        return "", ""


def do_one(team, vlan):
    gw = "10.42.%d.1" % vlan
    ip = discover(vlan)
    if not ip:
        return (team, "NO-TELNET")
    if not topen(ip, 23):
        return (team, "not-cool")
    with DEPLOY_LOCK:
        try:
            deploy85.deploy(ip, gw, gw, True, "stage1.sh")
        except Exception as e:
            return (team, "swap-err:%s" % str(e)[:30])
        try:
            with Phone(ip) as p:
                is_stage1 = p.run("grep -c udisk1 /phone/telepwn/divert.sh", wait=8)
                if "1" not in is_stage1:
                    return (team, "swap-unverified")
                p.s.sendall(b"reboot\n")
                time.sleep(3)
        except Exception as e:
            return (team, "reboot-err:%s" % str(e)[:30])
    # wait for OTA cycle, re-discover IP (may change on re-DHCP)
    t0 = time.time()
    while time.time() - t0 < 360:
        nip = discover(vlan)
        if nip and topen(nip, 23):
            v, tm = ver_team(nip)
            if v == "3":
                return (team, "v3/%s@%s" % (tm or "?", nip))
        time.sleep(8)
    return (team, "no-v3-timeout")


def main():
    results = {}
    for i in range(0, len(TARGETS), WAVE):
        chunk = TARGETS[i:i + WAVE]
        log("=== wave %d: %s ===" % (i // WAVE + 1, [t for t, _ in chunk]))
        with cf.ThreadPoolExecutor(max_workers=len(chunk)) as ex:
            futs = {ex.submit(do_one, t, v): t for t, v in chunk}
            for f in cf.as_completed(futs):
                team = futs[f]
                res = f.result()
                results[team] = res[1]
                log("   %-16s -> %s" % (team, res[1]))
    log("=== SUMMARY ===")
    ok = [t for t, r in results.items() if r.startswith("v3")]
    bad = [(t, r) for t, r in results.items() if not r.startswith("v3")]
    for t, r in sorted(results.items()):
        log("   %-16s %s" % (t, r))
    log("v3: %d/%d  failed: %s" % (len(ok), len(TARGETS), bad))


if __name__ == "__main__":
    main()
