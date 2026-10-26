#!/usr/bin/env python3
"""Minimal telnet shell driver for the busybox phone.

Speaks just enough of the telnet protocol (refuses all IAC option
negotiation) to drive the interactive `#` shell non-interactively.

Usage:
    phonesh.py [--host H] [--port P] [--timeout S] CMD [CMD ...]

Each CMD is run in order; stdout of each is printed separated by markers.
"""
import argparse
import socket
import sys
import time

IAC = 0xFF
DONT, DO, WONT, WILL = 0xFE, 0xFD, 0xFC, 0xFB
SB, SE = 0xFA, 0xF0


def negotiate(sock, data):
    """Strip telnet IAC sequences from data, refusing all options."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == IAC and i + 1 < n:
            cmd = data[i + 1]
            if cmd in (DO, DONT, WILL, WONT) and i + 2 < n:
                opt = data[i + 2]
                resp = WONT if cmd in (DO, DONT) else DONT
                sock.sendall(bytes([IAC, resp, opt]))
                i += 3
                continue
            if cmd == SB:
                j = i + 2
                while j + 1 < n and not (data[j] == IAC and data[j + 1] == SE):
                    j += 1
                i = j + 2
                continue
            i += 2
            continue
        out.append(b)
        i += 1
    return bytes(out)


def read_until(sock, needle, timeout):
    buf = bytearray()
    deadline = time.time() + timeout
    sock.settimeout(0.5)
    while timeout == 0 or time.time() < deadline:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            if needle in buf:
                break
            continue
        if not chunk:
            break
        buf += negotiate(sock, chunk)
        if needle in buf:
            break
    return bytes(buf)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.69.200")
    ap.add_argument("--port", type=int, default=23)
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("cmds", nargs="+")
    args = ap.parse_args()

    if args.timeout:
        sock = socket.create_connection((args.host, args.port), timeout=args.timeout)
    else:
        sock = socket.create_connection((args.host, args.port))

    read_until(sock, b"# ", args.timeout)

    for cmd in args.cmds:
        # busybox here has no printf; rely on the shell prompt ("# ") as the
        # end-of-output sentinel rather than an echoed marker.
        sock.sendall((cmd + "\necho __END_TOKEN__\n").encode())
        out = read_until(sock, b"__END_TOKEN__", args.timeout)
        text = out.decode("utf-8", "replace")
        # Drop the echoed command line and the trailing prompt.
        if "\n" in text:
            text = text.split("\n", 1)[1]
        idx = text.rfind("__END_TOKEN__")
        if idx != -1:
            text = text[:idx]
        print(f"===CMD: {cmd}===")
        print(text.strip("\r\n"))
    sock.close()


if __name__ == "__main__":
    main()
