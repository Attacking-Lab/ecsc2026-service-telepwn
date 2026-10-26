#!/usr/bin/env python3
"""Reverse-nc file uploader for the phone (same method as tools/binup, but with a
configurable destination path).

The host binds a one-shot TCP listener and serves the file; the phone pulls it with
`busybox nc <host> <port> > dest`. Works on this phone (has nc) and needs only that the
phone can reach the host (it can: host is the on-link provisioning server).

Usage:
  push.py PHONE_IP LOCAL REMOTE [--host-ip 10.42.0.1] [--port 9008] [--chmod]
"""
import argparse
import os
import socket
import threading
import time

from ptelnet import Phone


def serve_once(path, bind_port, done):
    with open(path, "rb") as f:
        data = f.read()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", bind_port))
    srv.listen(1)
    srv.settimeout(30)
    try:
        conn, _ = srv.accept()
        conn.sendall(data)
        conn.shutdown(socket.SHUT_WR)
        conn.close()
        done["ok"] = True
        done["n"] = len(data)
    except Exception as e:
        done["err"] = str(e)
    finally:
        srv.close()


def _remote_size(listing):
    """Parse the byte size out of a busybox `ls -l` line, or -1 if it cannot."""
    for ln in listing.splitlines():
        parts = ln.split()
        if len(parts) >= 5 and parts[0].startswith(("-", "l")):
            try:
                return int(parts[4])
            except ValueError:
                continue
    return -1


def push_file(phone, local, remote, host_ip="10.42.0.1", port=9008, chmod=False, retries=4):
    """Upload one file to the phone via reverse-nc. `phone` is an open Phone().
    The busybox `nc` can lose the connect/bind race (leaving a 0-byte file) or truncate,
    so verify the *remote* size matches the local file and retry until it does.
    Returns (ok, bytes_served, remote_listing)."""
    local_size = os.path.getsize(local)
    remote_dir = os.path.dirname(remote)
    if remote_dir:
        phone.run(f"mkdir -p {remote_dir}")
    done = {}
    listing = ""
    for _ in range(retries):
        done = {}
        t = threading.Thread(target=serve_once, args=(local, port, done), daemon=True)
        t.start()
        time.sleep(0.5)
        cmd = f"busybox nc {host_ip} {port} > {remote}"
        if chmod:
            cmd += f"; chmod +x {remote}"
        phone.run(cmd, wait=60)
        t.join(timeout=35)
        listing = phone.run(f"ls -l {remote}").strip()
        if done.get("ok") and _remote_size(listing) == local_size:
            return True, done.get("n", 0), listing
        time.sleep(1)
    return False, done.get("n", 0), listing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phone_ip")
    ap.add_argument("local")
    ap.add_argument("remote", help="absolute destination path on the phone")
    ap.add_argument("--host-ip", default="10.42.0.1", help="IP the phone connects back to")
    ap.add_argument("--port", type=int, default=9008)
    ap.add_argument("--chmod", action="store_true", help="chmod +x the uploaded file")
    args = ap.parse_args()

    with Phone(args.phone_ip) as p:
        ok, n, listing = push_file(p, args.local, args.remote,
                                   args.host_ip, args.port, args.chmod)
    print(f"[+] served {n} bytes (local {os.path.getsize(args.local)})" if ok
          else "[-] serve failed")
    print(f"[*] remote: {listing}")


if __name__ == "__main__":
    main()
