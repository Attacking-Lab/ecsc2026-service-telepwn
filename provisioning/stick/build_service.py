"""Build the "service" stick firmware: the real game service (frontend), the
mediaup wallpaper/ringtone uploader, and everything they need, nothing else. Produces a
tarball in the same on-stick layout stage2.sh expects (fw/ contents at the tar root),
written to dist/<version>.tar.gz.

This is the only firmware builder; it bundles:
  frontend                    meta/service_src/target/arm-unknown-linux-musleabi/release
  frontend.debug              detached DWARF sidecar, shipped next to the binary
  mediaup                     tools/mediaup/target/arm-unknown-linux-musleabi/release
  anthems/                    anthems.zip            (per-team default ringtones)
  config/teams.json logos/..  tools/team_data_gen from team_meta.json
  alsa.conf snd_halaudio.ko   meta/service_src/frontend/alsa.conf + the ALSA card kernel module
  NOTICE                      service/NOTICE  (third-party license notices)
  team_index                  slug -> 0-based TEAM_ID (team_meta order)
  run                         the service launcher below (frontend + mediaup)
  stage2.sh fetch emac_fixed.ko server.txt version.txt   stick boot/OTA infra
"""
import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROV = os.path.dirname(HERE)
REPO = os.path.dirname(PROV)

EMAC = os.path.join(REPO, "tools", "emacfix", "emacfix.ko")
FETCH = os.path.join(PROV, "fetch", "target", "arm-unknown-linux-musleabi", "release",
                     "fetch")

SERVICE = os.path.join(REPO, "meta", "service_src")
ARM_REL = os.path.join(SERVICE, "target", "arm-unknown-linux-musleabi", "release")
FRONTEND = os.path.join(ARM_REL, "frontend")
MEDIAUP_CRATE = os.path.join(REPO, "tools", "mediaup")
MEDIAUP = os.path.join(MEDIAUP_CRATE, "target", "arm-unknown-linux-musleabi", "release",
                       "mediaup")
ALSA_CONF = os.path.join(SERVICE, "frontend", "alsa.conf")
NOTICE = os.path.join(REPO, "service", "NOTICE")
SND_HALAUDIO = os.path.join(os.path.dirname(REPO), "files", "snd_halaudio.ko")
ANTHEMS_ZIP = os.path.join(REPO, "anthems.zip")
TEAM_META = os.path.join(PROV, "content", "team_meta.json")
TEAMS_JSON = os.path.join(REPO, "teams.json")
TDG = os.path.join(REPO, "tools", "team_data_gen", "team_data_gen.py")
STAGE2 = os.path.join(HERE, "fw", "stage2.sh")
CLOCK = os.path.join(HERE, "fw", "clock.sh")
MOUNTWD = os.path.join(HERE, "fw", "mountwd.sh")

# anthem filename differences: team_data_gen derives <ringtone>.pcm from the team name,
# but a couple of anthem files are named differently in anthems.zip.
ANTHEM_ALIASES = {"usa.pcm": "united_states.pcm"}

OBJCOPY = os.environ.get("OBJCOPY") or next(
    (c for c in ("llvm-objcopy", "arm-linux-gnueabi-objcopy", "arm-none-eabi-objcopy",
                 "objcopy") if shutil.which(c)),
    "llvm-objcopy",
)


def split_debug(path, mode=0o755):
    """Strip an ELF and return (stripped (mode,bytes), DWARF sidecar (0o644,bytes)).

    The stripped binary keeps a .gnu_debuglink to "<name>.debug" (basename only), so a
    debugger re-associates them once the sidecar sits next to the binary in fw/."""
    name = os.path.basename(path)
    with tempfile.TemporaryDirectory() as td:
        binp = os.path.join(td, name)
        dbgp = os.path.join(td, name + ".debug")
        shutil.copy2(path, binp)
        subprocess.run([OBJCOPY, "--only-keep-debug", binp, dbgp], check=True)
        subprocess.run([OBJCOPY, "--strip-all", binp], check=True)
        subprocess.run([OBJCOPY, "--add-gnu-debuglink=" + name + ".debug", binp],
                       check=True, cwd=td)
        with open(binp, "rb") as f:
            stripped = f.read()
        with open(dbgp, "rb") as f:
            debug = f.read()
    return (mode, stripped), (0o644, debug)


def read_file(path, mode):
    with open(path, "rb") as f:
        return (mode, f.read())


def tarball(files):
    buf = io.BytesIO()
    now = int(time.time())
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=9,
                      format=tarfile.USTAR_FORMAT) as tar:
        dirs = set()
        for rel in sorted(files):
            parts = rel.split("/")[:-1]
            for i in range(1, len(parts) + 1):
                d = "/".join(parts[:i])
                if d not in dirs:
                    dirs.add(d)
                    info = tarfile.TarInfo(d)
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    info.mtime = now
                    tar.addfile(info)
            mode, data = files[rel]
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = mode & 0o7777
            info.mtime = now
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def write_tree(root, files):
    for rel, (mode, data) in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        os.chmod(path, mode & 0o7777)


RUN = """#!/bin/sh
# telepwn v4 service launcher: runs the frontend (LCD + keys + audio) and mediaup
# (wallpaper/ringtone web uploader, :80). stage2 execs this with T_FW
# (ro fw dir), T_DATA (rw stick dir), T_RUN (tmpfs), T_SRV, ECSC_TEAM, ECSC_TEAM_ID.
T_FW="${T_FW:-/tmp/udisk1/telepwn/fw}"
T_DATA="${T_DATA:-/tmp/udisk1/telepwn/data}"
SERVICE_DATA="${T_FW%/fw}/service_data"

# disable the native hpd hot-plug daemon: kill its respawn watchdog (svc_hpd.sh) first,
# then the server, so it does not come back.
for _p in $(ps | grep svc_hpd | grep -v grep | awk '{print $1}'); do kill "$_p" 2>/dev/null; done
killall hpdserver 2>/dev/null

# this phone's numeric team id from the gateway (/team_id -> ECSC_TEAM_ID in stage2);
# fall back to the team_index slug->id table only when the gateway gave none (offline).
TEAM_ID="${ECSC_TEAM_ID:-}"
if [ -z "$TEAM_ID" ] && [ -n "$ECSC_TEAM" ]; then
    TEAM_ID=$(awk -v t="$ECSC_TEAM" '$1==t{print $2}' "$T_FW/team_index" 2>/dev/null)
fi
[ -n "$TEAM_ID" ] || TEAM_ID=0
export TEAM_ID

# ---- audio: register ALSA card 0 "halaudio" ----
# stage2 already loaded the stock HAL stack (amxr..ept_driver) on this boot, but load it
# here too (idempotent) in case we are restarted without it, then the ALSA shim.
for _m in amxr halaudio halaudio_apm pxcLdxSupport pxcLdxHausware ept_driver; do
    grep -q "^${_m} " /proc/modules 2>/dev/null || insmod "/boot/driver/${_m}.ko" 2>/dev/null
done
grep -q "^snd_halaudio " /proc/modules 2>/dev/null || insmod "$T_FW/snd_halaudio.ko" 2>/dev/null
# libasound looks under /dev/snd; the phone has no udev, so link the card's nodes there.
if [ ! -e /dev/snd/controlC0 ]; then
    mkdir -p /dev/snd
    ln -sf /dev/controlC0 /dev/snd/ 2>/dev/null
    for _n in /dev/pcmC*; do ln -sf "$_n" /dev/snd/ 2>/dev/null; done
fi

# The services run from the fw dir, so the read-only config/ anthems/ alsa.conf
# resolve there directly. Their writable data lives on the stick in service_data (the
# frontend creates frontend/..., under it), reached through
# the fixed path /tmp/telepwn_data (/ is ro, so the link itself lives on tmpfs). Replace
# any existing link first: busybox `ln -sf X /tmp/telepwn_data` follows an existing
# symlink-to-dir and drops the link *inside* it (/tmp/telepwn_data/service_data -> ...),
# so a second launch this boot would otherwise nest a self-referential link in the data.
cd "$T_FW" || exit 1
mkdir -p "$SERVICE_DATA"
rm -rf /tmp/telepwn_data
ln -s "$SERVICE_DATA" /tmp/telepwn_data

export ALSA_CONFIG_PATH="$T_FW/alsa.conf"

# Each restart of a service decides afresh where its output goes: a per-service file under
# service_data/log on the stick when the sentinel file $LOG_FLAG exists, else /dev/null (no
# writes to the stick). So you can telnet in, `touch` the flag, kill a service, and its
# supervisor brings it back logging to disk (rm the flag + restart to stop). Logs never live
# on tmpfs: /tmp is RAM.
LOG_DIR="$T_DATA/log"
LOG_FLAG="$T_DATA/log_to_disk"

logdest() {
    if [ -e "$LOG_FLAG" ]; then
        mkdir -p "$LOG_DIR"
        echo "$LOG_DIR/$1.log"
    else
        echo /dev/null
    fi
}

sh "$T_FW/clock.sh" >/dev/null 2>&1 &
sh "$T_FW/mountwd.sh" >/dev/null 2>&1 &

killall telnetd 2>/dev/null
( while : ; do telnetd -F -l /bin/sh >/dev/null 2>&1; sleep 2; done ) &

( while : ; do cd "$T_FW"; log=$(logdest frontend); ./frontend >>"$log" 2>&1; echo "frontend exited $?" >>"$log"; sleep 2; done ) &
"$T_FW/mediaup" >/dev/null 2>&1 &
wait
"""


def slug_order():
    """team_meta order == 0-based TEAM_ID (and team_data_gen's enumerate order)."""
    return list(json.load(open(TEAM_META)).keys())


def config_from_dir(cfg):
    """Pack a pre-generated config/ tree (teams.json + logos/ + wallpapers/ + anthems/).
    Source archives like anthems.zip are skipped -- only the unpacked files ship."""
    files = {}
    for root, _, names in os.walk(cfg):
        for n in names:
            if n.endswith(".zip"):
                continue
            p = os.path.join(root, n)
            files["config/" + os.path.relpath(p, cfg)] = (0o644, open(p, "rb").read())
    return files


def gen_config(tmp):
    """Run team_data_gen on the scoreboard teams.json to produce
    config/{teams.json,logos,wallpapers}; team_id/name/logo come from there, so the
    generated teams.json "id" matches the gateway's /team_id (needs Pillow)."""
    teams = json.load(open(TEAMS_JSON))["teams"]
    tdg_in = os.path.join(tmp, "tdg_input.json")
    json.dump({"teams": [{"team_id": t["team_id"], "name": t["name"], "logo": t["logo"]}
                         for t in teams]}, open(tdg_in, "w"))
    cfg = os.path.join(tmp, "config")
    os.makedirs(os.path.join(cfg, "logos"), exist_ok=True)
    os.makedirs(os.path.join(cfg, "wallpapers"), exist_ok=True)
    subprocess.run([sys.executable, TDG, tdg_in, cfg], check=True, stdout=subprocess.DEVNULL)
    return config_from_dir(cfg)


def anthem_files():
    """Default ringtones under config/anthems/ -- teams.json points each team's
    default_ringtone_path at anthems/<name>.pcm, resolved relative to config/."""
    files = {}
    with zipfile.ZipFile(ANTHEMS_ZIP) as z:
        members = {os.path.basename(n): n for n in z.namelist() if n.endswith(".pcm")}
        for name, src in members.items():
            files[f"config/anthems/{name}"] = (0o644, z.read(src))
        for want, have in ANTHEM_ALIASES.items():
            if want not in members and have in members:
                files[f"config/anthems/{want}"] = (0o644, z.read(members[have]))
    return files


def ensure_nix_shell():
    """The frontend cross-link needs the ARM ALSA pkg-config env that only the repo-root
    shell.nix exports, so require it before building."""
    if not os.environ.get("IN_NIX_SHELL"):
        print(f"error: not in a nix-shell; run `nix-shell` in the repo root ({REPO}) first",
              file=sys.stderr)
        sys.exit(1)
    if not os.environ.get("PKG_CONFIG_PATH_arm_unknown_linux_musleabi"):
        print("error: wrong nix-shell; enter the repo-root shell.nix (missing the ARM ALSA "
              "cross env the frontend needs)", file=sys.stderr)
        sys.exit(1)


def rebuild_binaries():
    """Always rebuild the ARM frontend and mediaup in release mode before
    bundling. Each crate carries its own .cargo/config.toml pinning the
    arm-unknown-linux-musleabi target, so cargo is run from the crate directory."""
    ensure_nix_shell()
    for crate_dir in (os.path.join(SERVICE, "frontend"), MEDIAUP_CRATE):
        subprocess.run(["cargo", "build", "--release"], cwd=crate_dir, check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", type=int, required=True)
    ap.add_argument("--server", default="10.42.0.1:9003")
    ap.add_argument("--out", required=True, help="output tarball path")
    ap.add_argument("--tree", help="also write the unpacked tree here")
    ap.add_argument("--config-dir",
                    help="pre-built config/ tree to bundle as-is (must include anthems/); "
                         "default: generate it with team_data_gen from team_meta.json")
    args = ap.parse_args()

    rebuild_binaries()

    for p in (FRONTEND, MEDIAUP, ALSA_CONF, NOTICE, SND_HALAUDIO,
              TEAM_META, TEAMS_JSON, ANTHEMS_ZIP, TDG, EMAC, FETCH, STAGE2, CLOCK,
              MOUNTWD):
        if not os.path.exists(p):
            print(f"missing: {p}", file=sys.stderr)
            return 1

    files = {}
    files["frontend"], files["frontend.debug"] = split_debug(FRONTEND)
    files["mediaup"] = read_file(MEDIAUP, 0o755)
    files["run"] = (0o755, RUN.encode())
    files["stage2.sh"] = read_file(STAGE2, 0o755)
    files["clock.sh"] = read_file(CLOCK, 0o755)
    files["mountwd.sh"] = read_file(MOUNTWD, 0o755)
    files["alsa.conf"] = read_file(ALSA_CONF, 0o644)
    files["NOTICE"] = read_file(NOTICE, 0o644)
    files["snd_halaudio.ko"] = read_file(SND_HALAUDIO, 0o644)
    files["fetch"] = read_file(FETCH, 0o755)
    files["emac_fixed.ko"] = read_file(EMAC, 0o644)
    files["server.txt"] = (0o644, f"{args.server}\n".encode())
    files["version.txt"] = (0o644, f"{args.version}\n".encode())
    files["team_index"] = (0o644, "".join(f"{s} {i}\n" for i, s in enumerate(slug_order())).encode())
    if args.config_dir:
        files.update(config_from_dir(args.config_dir))
    else:
        with tempfile.TemporaryDirectory() as td:
            files.update(gen_config(td))
        files.update(anthem_files())

    tar = tarball(files)
    with open(args.out, "wb") as f:
        f.write(tar)
    with open(args.out.replace(".tar.gz", ".sha256"), "w") as f:
        f.write(hashlib.sha256(tar).hexdigest() + "\n")
    if args.tree:
        write_tree(args.tree, files)

    print(f"[service-fw] v{args.version}: {len(files)} files, {len(tar)} bytes -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
