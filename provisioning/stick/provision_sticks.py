#!/usr/bin/env python3
"""Batch-provision telepwn USB sticks on the dev machine.

Waits for a USB stick to be plugged in and provisions it, then waits for it to be
unplugged and loops for the next one -- a whole crate without touching the keyboard.

Per stick:
  1. if the stick has more than one partition, or no single ext2, lay down one
     ext2 spanning the whole stick; otherwise reuse the ext2 already there
  2. mount it
  3. delete every file on it
  4. install the firmware named by dist/latest the way an OTA update does
     (unpack into fw.new, verify version.txt + stage2.sh, swap it in as fw/)
  5. unmount it
  6. zerofree the partition (zero all free blocks so nothing deleted survives)
  7. report done

The stick layout written is the one stage 2 expects (see stick/prepare.py and the
t_update() swap in fw/stage2.sh):

    telepwn/fw/     unpacked firmware tarball (stage2.sh, run, fetch,
                     emac_fixed.ko, server.txt, version.txt, ...) -- built by
                     stick/build_service.py
    telepwn/data/   empty, read/write (backend state, boot.log)

The firmware is read once from stick/dist/<version>.tar.gz (default: dist/latest)
and its sha256 is verified before the first stick is touched.

SAFETY: only a device that is (1) USB / removable, (2) appeared *after* this
script started, (3) not the disk backing / and (4) within the size bounds is ever
considered. The target is printed with a cancellable countdown before any
destructive step. Run with --list to see what it would pick, or --dry-run to go
through the motions without changing anything.

By default sticks are done one at a time (plug one, it provisions, unplug, repeat).
With --parallel every qualifying stick plugged in is provisioned at once, each on
its own worker thread, so a hub-full goes together -- the fast way through a crate.

Run (needs root for mkfs/mount/zerofree, and a built dist):
    sudo python3 stick/provision_sticks.py
    sudo python3 stick/provision_sticks.py --version 3 --yes
    sudo python3 stick/provision_sticks.py --parallel --yes
    python3 stick/provision_sticks.py --list
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DIST = os.path.join(HERE, "dist")
GiB = 1024 ** 3

_tls = threading.local()


def log(msg):
    tag = getattr(_tls, "tag", "")
    print(f"{time.strftime('%H:%M:%S')}  {tag}{msg}", flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)


def try_run(cmd):
    try:
        subprocess.run(cmd, check=False, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        pass


# ---------- firmware source ----------
def resolve_firmware(dist, version):
    if version is None:
        try:
            with open(os.path.join(dist, "latest")) as f:
                version = int(f.read().strip())
        except (OSError, ValueError):
            sys.exit(f"[-] no {dist}/latest; build a firmware first: python3 "
                     f"{os.path.join('stick', 'build_service.py')}")
    tgz = os.path.join(dist, f"{version}.tar.gz")
    shafile = os.path.join(dist, f"{version}.sha256")
    if not os.path.exists(tgz):
        sys.exit(f"[-] {tgz} does not exist (build it: stick/build_service.py --version {version})")
    blob = open(tgz, "rb").read()
    got = hashlib.sha256(blob).hexdigest()
    if os.path.exists(shafile):
        want = open(shafile).read().strip().split()[0]
        if want != got:
            sys.exit(f"[-] {tgz} sha256 mismatch: {got} != {want}")
    return version, tgz, got


# ---------- block device discovery ----------
def disks():
    out = run(["lsblk", "-J", "-b", "-o",
               "NAME,PATH,RM,TRAN,HOTPLUG,SIZE,TYPE,MOUNTPOINT,MODEL,SERIAL"]).stdout
    return {d["name"]: d for d in json.loads(out)["blockdevices"] if d["type"] == "disk"}


def _subtree_has_root(node):
    mp = node.get("mountpoint")
    mps = node.get("mountpoints") or []
    if mp == "/" or "/" in mps:
        return True
    return any(_subtree_has_root(c) for c in node.get("children", []))


def root_disks():
    """Set of whole-disk names whose subtree backs / (through partitions, LUKS,
    LVM, ...). These are never touched, belt-and-suspenders on top of the baseline."""
    out = run(["lsblk", "-J", "-o", "NAME,TYPE,MOUNTPOINT,MOUNTPOINTS"]).stdout
    roots = set()
    for d in json.loads(out)["blockdevices"]:
        if d.get("type") == "disk" and _subtree_has_root(d):
            roots.add(d["name"])
    return roots


def is_removable_usb(d):
    return d.get("tran") == "usb" or (str(d.get("rm")) == "1" and str(d.get("hotplug")) == "1")


def describe(d):
    gb = (d.get("size") or 0) / GiB
    return (f"{d['path']}  {gb:.1f} GiB  tran={d.get('tran')}  "
            f"model={(d.get('model') or '').strip()!r}  serial={d.get('serial')}")


def candidates(baseline, root, min_b, max_b):
    out = []
    for name, d in disks().items():
        if name in baseline or name in root:
            continue
        if not is_removable_usb(d):
            continue
        size = d.get("size") or 0
        if size < min_b or size > max_b:
            log(f"[skip] {describe(d)} -- size out of bounds")
            continue
        out.append(d)
    return out


def wait_for_candidate(baseline, root, min_b, max_b, poll):
    log("waiting for a USB stick…  (plug one in; Ctrl-C to stop)")
    while True:
        cands = candidates(baseline, root, min_b, max_b)
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1:
            log(f"[!] {len(cands)} new USB disks present; unplug all but one "
                f"({', '.join(c['path'] for c in cands)})")
            while len(candidates(baseline, root, min_b, max_b)) != 1:
                time.sleep(poll)
            return candidates(baseline, root, min_b, max_b)[0]
        time.sleep(poll)


def wait_until_gone(name, poll):
    while name in disks():
        time.sleep(poll)


# ---------- format + seed ----------
def part_node(disk_path):
    base = os.path.basename(disk_path)
    sep = "p" if base[-1].isdigit() else ""
    return f"{disk_path}{sep}1"


def confirm(d, delay):
    print(f"\n==> TARGET: {describe(d)}")
    print(f"    This ERASES the device. Ctrl-C within {delay}s to abort.", flush=True)
    for i in range(delay, 0, -1):
        print(f"    provisioning in {i}… ", end="\r", flush=True)
        time.sleep(1)
    print(" " * 40, end="\r")


def disk_tree(disk_path):
    out = run(["lsblk", "-J", "-b", "-o", "NAME,PATH,TYPE,FSTYPE", "--", disk_path]).stdout
    return json.loads(out)["blockdevices"][0]


def existing_ext2(disk_path, whole_disk):
    """Return the fsnode to reuse, or None if the stick must be reformatted.

    Reformat when there is more than one partition, or no single ext2 that already
    spans the whole stick."""
    node = disk_tree(disk_path)
    parts = [c for c in node.get("children", []) if c.get("type") == "part"]
    if whole_disk:
        if not parts and node.get("fstype") == "ext2":
            return disk_path
        return None
    if len(parts) == 1 and parts[0].get("fstype") == "ext2":
        return parts[0]["path"]
    return None


def make_ext2(disk_path, label, whole_disk):
    log(f"wiping signatures on {disk_path}")
    run(["wipefs", "-a", disk_path])
    if whole_disk:
        fsnode = disk_path
    else:
        log(f"creating one Linux partition spanning {disk_path}")
        subprocess.run(["sfdisk", disk_path], input="label: dos\n,,L\n",
                       text=True, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try_run(["partprobe", disk_path])
        try_run(["udevadm", "settle"])
        fsnode = part_node(disk_path)
        for _ in range(20):
            if os.path.exists(fsnode):
                break
            time.sleep(0.25)
        else:
            raise RuntimeError(f"partition {fsnode} never appeared")
    log(f"mkfs.ext2 on {fsnode} (label {label})")
    run(["mkfs.ext2", "-F", "-q", "-m", "0", "-L", label, fsnode])
    return fsnode


def mount_fs(fsnode):
    mnt = tempfile.mkdtemp(prefix="qstick.")
    try:
        run(["mount", fsnode, mnt])
    except Exception:
        try:
            os.rmdir(mnt)
        except OSError:
            pass
        raise
    return mnt


def umount_fs(fsnode, mnt):
    subprocess.run(["umount", fsnode], check=False)
    try_run(["udevadm", "settle"])
    try:
        os.rmdir(mnt)
    except OSError:
        pass


def delete_all(mnt):
    log(f"deleting every file on {mnt}")
    for entry in os.listdir(mnt):
        if entry == "lost+found":
            continue
        run(["rm", "-rf", "--", os.path.join(mnt, entry)])


def apply_ota(mnt, tgz, version):
    """Install fw/ the way stage2.sh's t_update() does: unpack into fw.new, verify
    version.txt + stage2.sh, then atomically swap it in as fw/."""
    root = os.path.join(mnt, "telepwn")
    data = os.path.join(root, "data")
    fw = os.path.join(root, "fw")
    fw_new = os.path.join(root, "fw.new")
    fw_old = os.path.join(root, "fw.old")
    os.makedirs(data, exist_ok=True)
    run(["rm", "-rf", "--", fw_new])
    os.makedirs(fw_new)
    log(f"unpacking firmware {version} -> telepwn/fw.new/")
    run(["tar", "xzf", tgz, "-C", fw_new])
    vfile = os.path.join(fw_new, "version.txt")
    got = open(vfile).read().strip() if os.path.exists(vfile) else ""
    if got != str(version) or not os.path.exists(os.path.join(fw_new, "stage2.sh")):
        run(["rm", "-rf", "--", fw_new])
        raise RuntimeError(f"ota verify failed: version={got!r}")
    run(["rm", "-rf", "--", fw_old])
    if os.path.exists(fw):
        os.rename(fw, fw_old)
    try:
        os.rename(fw_new, fw)
    except OSError:
        if os.path.exists(fw_old):
            os.rename(fw_old, fw)
        raise
    run(["rm", "-rf", "--", fw_old])
    os.sync()
    log(f"firmware {version} installed via OTA swap")


def zerofree_node(fsnode):
    log(f"zerofree -v {fsnode} (zeroing free blocks)")
    out = run(["zerofree", "-v", fsnode]).stdout or ""
    last = out.replace("\r", "\n").strip().splitlines()[-1:] or [""]
    parts = last[0].split("/")
    if len(parts) == 3 and all(p.strip().isdigit() for p in parts):
        mod, free, total = (int(p) for p in parts)
        pct = 100.0 * mod / free if free else 0.0
        log(f"zerofree done: rewrote {mod}/{free} free blocks "
            f"({pct:.0f}% of free, ~{mod * 4096 / GiB:.2f} GiB @4K) of {total} total")
    else:
        log(f"zerofree done: {last[0]}")


def provision_one(d, args, tgz, version):
    """The 7-step flow on one stick. Returns True on success. Used by both the
    serial loop and the parallel worker pool."""
    path = d["path"]
    try:
        reuse = existing_ext2(path, args.whole_disk)
        if reuse:
            log(f"reusing existing whole-stick ext2 at {reuse}")
            fsnode = reuse
        else:
            log("not a single whole-stick ext2 -> laying down a fresh one")
            fsnode = make_ext2(path, args.label, args.whole_disk)
        mnt = mount_fs(fsnode)
        try:
            delete_all(mnt)
            apply_ota(mnt, tgz, version)
            os.sync()
        finally:
            umount_fs(fsnode, mnt)
        zerofree_node(fsnode)
        print("\a", end="")
        log(f"✅ DONE: {path} -> ext2 {args.label}, firmware v{version}, free space zeroed.")
        return True
    except (subprocess.CalledProcessError, RuntimeError, OSError) as e:
        log(f"❌ FAILED on {path}: {e}")
        return False


def run_parallel(baseline, root, min_b, max_b, args, tgz, version):
    """Provision every qualifying stick concurrently, one worker thread each, and
    keep picking up newly-plugged sticks while others are still running."""
    inflight = {}
    lock = threading.Lock()
    tally = {"ok": 0, "fail": 0}

    def worker(d):
        name = d["name"]
        _tls.tag = f"[{name}] "
        ok = provision_one(d, args, tgz, version)
        with lock:
            tally["ok" if ok else "fail"] += 1
            log(f"done ({tally['ok']} ok, {tally['fail']} failed). Unplug it.")
        wait_until_gone(name, args.poll)
        with lock:
            inflight.pop(name, None)

    log(f"PARALLEL mode (≤{args.max_workers} at once): plug in as many sticks as you "
        f"like; each qualifying one is wiped + provisioned automatically. Ctrl-C to stop.")
    while True:
        for d in candidates(baseline, root, min_b, max_b):
            name = d["name"]
            with lock:
                if name in inflight or len(inflight) >= args.max_workers:
                    continue
                inflight[name] = d
            threading.Thread(target=worker, args=(d,), daemon=True).start()
        time.sleep(args.poll)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default=DEFAULT_DIST)
    ap.add_argument("--version", type=int, help="firmware version (default: latest)")
    ap.add_argument("--label", default="TELEPWN")
    ap.add_argument("--whole-disk", action="store_true",
                    help="mkfs the whole device instead of a single partition")
    ap.add_argument("--min-size-gb", type=float, default=1.0)
    ap.add_argument("--max-size-gb", type=float, default=256.0)
    ap.add_argument("--delay", type=int, default=5, help="abort countdown before format")
    ap.add_argument("--yes", action="store_true", help="skip the countdown (unattended)")
    ap.add_argument("--poll", type=float, default=1.0)
    ap.add_argument("--parallel", action="store_true",
                    help="provision every plugged stick at once (implies --yes)")
    ap.add_argument("--max-workers", type=int, default=16,
                    help="max sticks provisioned concurrently in --parallel")
    ap.add_argument("--list", action="store_true",
                    help="print current USB disks and exit (no changes)")
    ap.add_argument("--dry-run", action="store_true",
                    help="detect + print, but do not format or write")
    args = ap.parse_args()

    root = root_disks()
    min_b, max_b = int(args.min_size_gb * GiB), int(args.max_size_gb * GiB)

    if args.list:
        log(f"root disk(s): {', '.join(sorted(root)) or '?'} (never touched)")
        for name, d in disks().items():
            tag = "USB" if is_removable_usb(d) else "fixed"
            flag = " <ROOT>" if name in root else ""
            print(f"  [{tag}] {describe(d)}{flag}")
        return 0

    if not args.dry_run and os.geteuid() != 0:
        sys.exit("[-] must run as root (mkfs/mount). Re-run under sudo.")

    version, tgz, sha = resolve_firmware(args.dist, args.version)
    log(f"firmware version {version}, {os.path.getsize(tgz)} bytes, sha256 {sha[:16]}…")
    log(f"root disk(s): {', '.join(sorted(root)) or '?'} (never touched). "
        f"ext2 label {args.label}. "
        f"size {args.min_size_gb}-{args.max_size_gb} GiB.")

    baseline = set(disks())
    log(f"baseline disks (ignored): {', '.join(sorted(baseline)) or 'none'}")

    if args.parallel and not args.dry_run:
        if not args.yes:
            sys.exit("[-] --parallel auto-wipes any qualifying stick without a per-stick "
                     "countdown; re-run with --yes to confirm you want that.")
        return run_parallel(baseline, root, min_b, max_b, args, tgz, version)

    n = 0
    while True:
        d = wait_for_candidate(baseline, root, min_b, max_b, args.poll)
        name = d["name"]
        if args.dry_run:
            log(f"[dry-run] would wipe + provision {describe(d)}")
            log("[dry-run] unplug it to continue, or Ctrl-C to stop")
            wait_until_gone(name, args.poll)
            continue
        if not args.yes:
            try:
                confirm(d, args.delay)
            except KeyboardInterrupt:
                log("aborted this stick; unplug it to continue")
                wait_until_gone(name, args.poll)
                continue
        if provision_one(d, args, tgz, version):
            n += 1
            log(f"#{n} done. Unplug it; next stick please.")
        else:
            log("unplug it and retry.")
        wait_until_gone(name, args.poll)


if __name__ == "__main__":
    sys.exit(main())
