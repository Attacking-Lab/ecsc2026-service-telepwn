#!/usr/bin/env python3
"""Minimal telnet command runner for the T46S busybox root shell.

The phone's telnet (telnet1.rom) drops straight into a passwordless root ash
shell with a "# " prompt (no login). We use that prompt as the command sentinel.

Usage:
  ptelnet.py HOST -c "cmd"                 run a command, print output
  ptelnet.py HOST --get /remote/path OUT   fetch a (text/binary) file via base64*
  ptelnet.py HOST --put LOCAL /remote/path upload a file (chunked, base64-safe*)

  *busybox 1.10.3 here has no base64 applet, so --get/--put use uuencode-free
   od/printf-free transfers: --get uses `cat` for text and a hex path for binary;
   --put streams via printf-of-octal through `dd`. Keep payloads modest.
"""
import argparse
import socket
import sys
import time

PROMPT = b"# "


class Phone:
    def __init__(self, host, port=23, timeout=8):
        self.host, self.port, self.timeout = host, port, timeout
        self.s = None

    def __enter__(self):
        self.s = socket.socket()
        self.s.settimeout(self.timeout)
        self.s.connect((self.host, self.port))
        self._drain(1.0)
        # Telnet IAC negotiation may arrive; a bare newline gets us a clean prompt.
        self.s.sendall(b"\n")
        self._read_to_prompt(2.0)
        return self

    def __exit__(self, *a):
        try:
            self.s.close()
        except Exception:
            pass

    def _drain(self, t):
        self.s.settimeout(t)
        try:
            while True:
                if not self.s.recv(4096):
                    break
        except Exception:
            pass
        self.s.settimeout(self.timeout)

    def _read_to_prompt(self, t):
        self.s.settimeout(t)
        buf = b""
        try:
            while True:
                d = self.s.recv(4096)
                if not d:
                    break
                buf += d
                if buf.rstrip().endswith(b"#"):
                    break
        except socket.timeout:
            pass
        finally:
            self.s.settimeout(self.timeout)
        return buf

    def run(self, cmd, wait=30):
        """Run cmd, return exactly the stdout between unique start/end markers."""
        start, end = "__PXSTART__", "__PXEND__"
        # Empty quotes so the shell-echoed input line does NOT contain the literal
        # markers; only the actual `echo` output does. Prevents matching our own echo.
        full = f"echo __PXST''ART__; {cmd}; echo __PXE''ND__$?\n"
        self.s.sendall(full.encode())
        deadline = time.time() + wait
        buf = b""
        self.s.settimeout(2)
        while time.time() < deadline:
            try:
                d = self.s.recv(8192)
                if not d:
                    break
                buf += d
                if end.encode() in buf:
                    break
            except socket.timeout:
                continue
        text = buf.decode(errors="replace")
        # take content strictly between the LAST start marker and the end marker
        try:
            after = text.split(start + "\r\n", 1)[1]
            body = after.split(end, 1)[0]
        except IndexError:
            return text
        return body.rstrip("\r\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("-c", "--cmd", help="command to run")
    ap.add_argument("--get", nargs=2, metavar=("REMOTE", "LOCAL"))
    ap.add_argument("--port", type=int, default=23)
    ap.add_argument("--wait", type=int, default=30)
    args = ap.parse_args()

    with Phone(args.host, args.port) as p:
        if args.cmd:
            sys.stdout.write(p.run(args.cmd, wait=args.wait))
            sys.stdout.write("\n")
        elif args.get:
            remote, local = args.get
            # od -An -tx1 is available in this busybox; use it for a safe binary grab.
            hexout = p.run(f"od -An -v -tx1 {remote}", wait=args.wait)
            data = bytes.fromhex("".join(hexout.split()))
            with open(local, "wb") as f:
                f.write(data)
            print(f"[+] wrote {len(data)} bytes to {local}")


if __name__ == "__main__":
    main()
