#!/usr/bin/env python3
"""First-time setup of a phone's USB stick for the stick boot, over telnet.

Puts a published firmware version (stick/build_service.py) on the stick as telepwn/fw/ and
creates the read/write telepwn/data/. An existing telepwn/ that is not in this layout
is moved aside to telepwn.bak first. Later versions arrive through stage 2's update, so
this only has to run once per stick.

The phone must already have the stick mounted (dskPhone mounts it on /tmp/udisk1 in
normal mode; stage 1 mounts it there too).

    prepare.py PHONE_IP [--version N] [--host-ip 10.42.0.1]
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "hijack"))

from ptelnet import Phone  # noqa: E402
from push import push_file  # noqa: E402

STICK = "/tmp/udisk1"
ROOT = f"{STICK}/telepwn"


def latest(dist):
    with open(os.path.join(dist, "latest")) as f:
        return int(f.read().strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phone_ip")
    ap.add_argument("--dist", default=os.path.join(HERE, "dist"))
    ap.add_argument("--version", type=int, help="default: latest published")
    ap.add_argument("--host-ip", default="10.42.0.1", help="IP the phone connects back to")
    ap.add_argument("--port", type=int, default=9008)
    args = ap.parse_args()

    version = args.version if args.version is not None else latest(args.dist)
    tgz = os.path.join(args.dist, f"{version}.tar.gz")

    with Phone(args.phone_ip) as p:
        if f" {STICK} " not in p.run("cat /proc/mounts"):
            print(f"[-] no stick mounted on {STICK}")
            return 1
        layout = p.run(f"ls {ROOT} 2>/dev/null").split()
        if layout and "fw" not in layout:
            if "telepwn.bak" in p.run(f"ls {STICK}").split():
                print(f"[-] {ROOT} has an old layout and {STICK}/telepwn.bak exists")
                return 1
            p.run(f"mv {ROOT} {STICK}/telepwn.bak")
            print(f"[*] moved the old {ROOT} to {STICK}/telepwn.bak")

        ok, n, _ = push_file(p, tgz, f"{ROOT}/fw.tar.gz", args.host_ip, args.port)
        if not ok or n != os.path.getsize(tgz):
            print("[-] upload failed")
            return 1
        out = p.run(
            f"cd {ROOT} && rm -rf fw.new && mkdir fw.new && cd fw.new && "
            f"tar xzf ../fw.tar.gz && cd .. && rm -rf fw.old && "
            f"{{ [ ! -d fw ] || mv fw fw.old; }} && mv fw.new fw && "
            f"rm -rf fw.old fw.tar.gz && mkdir -p data && sync && cat fw/version.txt",
            wait=120,
        )
        got = out.strip().splitlines()[-1:] or [""]
        if got[0].strip() != str(version):
            print(f"[-] unpack failed: {out.strip()}")
            return 1
        print(f"[+] {ROOT}/fw is version {version}")
        print(p.run(f"ls {ROOT} {ROOT}/fw").strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
