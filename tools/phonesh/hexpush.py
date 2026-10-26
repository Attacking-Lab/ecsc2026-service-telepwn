#!/usr/bin/env python3
"""Push a binary to the busybox phone over the telnet shell only.

Needed when the phone cannot open connections back to the host (e.g. the host
reaches the phone through a Tailscale subnet route, so binup's reverse-nc and any
phone-initiated pull are impossible) and busybox `nc` is connect-only (cannot
listen). The one working channel is the host-initiated telnet shell.

busybox here has no base64/printf/xxd, and `awk`'s printf "%c" silently drops NUL
bytes, so we cannot text-decode a binary the naive way. Instead:

  host:  gzip -9 the file, hex-encode, stream the hex (printable, telnet-safe)
         into /tmp/m.hex via `cat > ...` + ^D.
  phone: one awk pass reads the hex and emits bytes; non-NUL via printf "%c",
         each NUL run via fflush()+system("dd if=/dev/zero bs=1 count=k") -- dd
         inherits awk's redirected stdout so the bytes land in order.
  verify: gzip -t (CRC) proves the transfer, then gzip -d yields the exact file.

Usage:
    hexpush.py <local_file> <remote_path> [--host H] [--port P]
"""
import argparse
import gzip
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from phonesh import negotiate, read_until  # noqa: E402

LINE = 120  # hex chars per line; keep well under the pty canonical limit

DECODER = [
    "BEGIN{",
    'hx="0123456789abcdef"; z=0',
    'while((getline line < "/tmp/m.hex")>0){',
    "n=length(line)",
    "for(i=1;i<=n;i+=2){",
    "a=index(hx,substr(line,i,1))-1; b=index(hx,substr(line,i+1,1))-1; v=a*16+b",
    'if(v==0){z++} else {if(z>0){fflush();system("dd if=/dev/zero bs=1 count=" z " 2>/dev/null");z=0}; printf "%c",v}',
    "}",
    "}",
    'if(z>0){fflush();system("dd if=/dev/zero bs=1 count=" z " 2>/dev/null")}',
    "}",
]


def send_cmd(sock, cmd, timeout=20.0):
    sock.sendall((cmd + "\n").encode())
    out = read_until(sock, b"# ", timeout)
    return out.decode("utf-8", "replace")


def stream_to_file(sock, remote, lines, timeout=600.0):
    """`cat > remote` then stream text lines then ^D, draining echo concurrently."""
    done = threading.Event()
    tail = bytearray()

    def drain():
        sock.settimeout(0.5)
        while not done.is_set():
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            tail.extend(negotiate(sock, chunk))
            del tail[:-4096]

    sock.sendall(f"cat > {remote}\n".encode())
    time.sleep(0.3)
    t = threading.Thread(target=drain, daemon=True)
    t.start()
    payload = ("\n".join(lines) + "\n").encode()
    view = memoryview(payload)
    step = 8192
    for off in range(0, len(payload), step):
        sock.sendall(view[off : off + step])
        time.sleep(0.01)
    sock.sendall(b"\x04")  # ^D -> EOF for cat
    done_deadline = time.time() + timeout
    while time.time() < done_deadline:
        if b"# " in tail:
            break
        time.sleep(0.2)
    done.set()
    t.join(timeout=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("local")
    ap.add_argument("remote")
    ap.add_argument("--host", default="192.168.69.200")
    ap.add_argument("--port", type=int, default=23)
    args = ap.parse_args()

    raw = open(args.local, "rb").read()
    gz = gzip.compress(raw, 9)
    hexstr = gz.hex()
    lines = [hexstr[i : i + LINE] for i in range(0, len(hexstr), LINE)]
    print(
        f"[*] {args.local}: {len(raw)} B -> gz {len(gz)} B -> {len(lines)} hex lines",
        flush=True,
    )

    sock = socket.create_connection((args.host, args.port), timeout=15)
    read_until(sock, b"# ", 15)
    gzpath = args.remote + ".gz"
    send_cmd(sock, f"rm -f /tmp/m.hex /tmp/dec.awk {gzpath} {args.remote}")

    print("[*] streaming hex ...", flush=True)
    stream_to_file(sock, "/tmp/m.hex", lines)
    nbytes = send_cmd(sock, "wc -c < /tmp/m.hex 2>/dev/null || ls -l /tmp/m.hex")
    print(f"[*] /tmp/m.hex: {nbytes.strip()} (expect {len(hexstr)} hex chars + {len(lines)} newlines)")

    print("[*] writing decoder + decoding ...", flush=True)
    stream_to_file(sock, "/tmp/dec.awk", DECODER)
    send_cmd(sock, f"awk -f /tmp/dec.awk > {gzpath}", timeout=180)
    test = send_cmd(sock, f"gzip -t {gzpath} && echo GZOK || echo GZBAD", timeout=60)
    if "GZOK" not in test:
        print(f"[!] gzip integrity check FAILED:\n{test}")
        sock.close()
        sys.exit(1)
    send_cmd(sock, f"gzip -d {gzpath}", timeout=60)
    send_cmd(sock, f"chmod +x {args.remote}")
    final = send_cmd(sock, f"ls -l {args.remote}")
    print(f"[+] decoded -> {final.strip()}")
    print(f"[+] expected size {len(raw)} B")
    send_cmd(sock, "rm -f /tmp/m.hex /tmp/dec.awk")
    sock.close()


if __name__ == "__main__":
    main()
