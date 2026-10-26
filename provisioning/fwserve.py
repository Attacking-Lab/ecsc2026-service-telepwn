#!/usr/bin/env python3
"""Team lookup and firmware update server for phones booting from their USB stick.

Stage 2 on the stick (stick/fw/stage2.sh) asks this server, whose address it reads from
fw/server.txt, three things over plain HTTP:

  GET /team             -> this phone's team slug. Like callback.py, the team is the
                           *source subnet* of the request (every team is on its own VLAN,
                           10.42.<vlan>.0/24 per vlans/vlanmap.csv), never anything the
                           phone sends. 404 when the subnet belongs to no team.
  GET /team_id          -> this phone's team id as a number (content/team_ids.csv: the
                           3rd octet of the team's 10.60.<id>.4 game address). 404 when
                           the subnet belongs to no team or the team has no id.
  GET /version          -> the newest firmware version (a plain integer).
  GET /fw/<v>.tar.gz    -> that firmware tree, as built by stick/build.py.
  GET /fw/<v>.sha256    -> its sha256, which the phone checks before unpacking.

Runs alongside callback.py on the provisioning server:

    fwserve.py [--port 9003] [--dist stick/dist] [--ids content/team_ids.csv]
               [--net 10.42.1=nop ...]

`--net PREFIX=SLUG` adds or overrides a subnet mapping, e.g. for a bench phone that is
not on a team VLAN.
"""
import argparse
import csv
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import callback

HERE = os.path.dirname(os.path.abspath(__file__))
FW_PATH = re.compile(r"^/fw/(\d+)\.(tar\.gz|sha256)$")


def load_team_ids(path):
    ids = {}
    try:
        with open(path, newline="") as f:
            for row in csv.reader(f):
                if len(row) >= 2 and row[1].strip().isdigit():
                    ids[row[0].strip()] = int(row[1])
    except FileNotFoundError:
        pass
    return ids


def latest_version(dist):
    try:
        with open(os.path.join(dist, "latest")) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "fwserve"
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def send_body(self, body, ctype="text/plain"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path):
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(404)
            return
        with f:
            size = os.fstat(f.fileno()).st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            while chunk := f.read(64 * 1024):
                self.wfile.write(chunk)

    def do_GET(self):
        peer = self.client_address[0]
        cfg = self.server.cfg
        if self.path == "/team":
            info = cfg["netmap"].get(callback.net_prefix(peer))
            if not info:
                self.send_error(404, "no team for this subnet")
                return
            self.send_body(f"{info['slug']}\n".encode())
            return
        if self.path == "/team_id":
            info = cfg["netmap"].get(callback.net_prefix(peer))
            tid = cfg["ids"].get(info["slug"]) if info else None
            if tid is None:
                self.send_error(404, "no team id for this subnet")
                return
            self.send_body(f"{tid}\n".encode())
            return
        if self.path == "/version":
            v = latest_version(cfg["dist"])
            if v is None:
                self.send_error(503, "no firmware published")
                return
            self.send_body(f"{v}\n".encode())
            return
        m = FW_PATH.match(self.path)
        if m:
            name = f"{m.group(1)}.{m.group(2)}"
            self.send_file(os.path.join(cfg["dist"], name))
            return
        self.send_error(404)


def parse_net(arg):
    pfx, sep, slug = arg.partition("=")
    if not sep or len(pfx.split(".")) != 3 or not slug:
        raise argparse.ArgumentTypeError("expected PREFIX=SLUG, e.g. 10.42.1=nop")
    return pfx, slug


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9003)
    ap.add_argument("--dist", default=os.path.join(HERE, "stick", "dist"))
    ap.add_argument("--vlanmap", default=os.path.join(HERE, "vlans", "vlanmap.csv"))
    ap.add_argument("--meta", default=os.path.join(HERE, "content", "team_meta.json"))
    ap.add_argument("--ids", default=os.path.join(HERE, "content", "team_ids.csv"))
    ap.add_argument("--net", type=parse_net, action="append", default=[])
    args = ap.parse_args()

    netmap = callback.load_netmap(args.vlanmap, args.meta)
    for pfx, slug in args.net:
        netmap[pfx] = {"slug": slug, "name": slug, "vlan": None}

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    ids = load_team_ids(args.ids)
    srv.cfg = {"netmap": netmap, "dist": args.dist, "ids": ids}
    print(f"fwserve on {args.host}:{args.port}, {len(netmap)} subnet(s), {len(ids)} team id(s), "
          f"dist={args.dist} latest={latest_version(args.dist)}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
