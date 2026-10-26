#!/usr/bin/env python3
"""Run a command on the phone in the foreground and hold the telnet session
open, streaming its output, until this process is terminated.

busybox here has no nohup/setsid, so a backgrounded job dies on session close.
Keeping the session open (foreground) keeps the process alive; closing it
(SIGTERM/SIGINT here) drops the pty and cleans up the remote process.

Usage:
    hold_run.py "CMD" [--host H] [--port P]
"""
import argparse
import signal
import socket
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from phonesh import negotiate, read_until  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd")
    ap.add_argument("--host", default="192.168.69.200")
    ap.add_argument("--port", type=int, default=23)
    args = ap.parse_args()

    sock = socket.create_connection((args.host, args.port), timeout=15)
    read_until(sock, b"# ", 15)
    sock.sendall((args.cmd + "\n").encode())

    stop = {"flag": False}

    def handle(_sig, _frm):
        stop["flag"] = True

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)

    sock.settimeout(0.5)
    while not stop["flag"]:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        data = negotiate(sock, chunk)
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    try:
        sock.close()
    except OSError:
        pass


if __name__ == "__main__":
    main()
