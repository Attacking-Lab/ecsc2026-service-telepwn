import socket
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "webflash"))
import yealink_web


def port_open(ip, port, timeout=3):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def main():
    ip = "10.42.1.153"
    rom = __import__("os").environ.get("TELNET1_ROM", "telnet1.rom")
    w = yealink_web.YealinkWeb(ip, timeout=30, verbose=True)
    w.login()
    print(f"[+] login OK (legacy={w.legacy})", flush=True)
    res = w.upload_firmware(rom, poll=False)
    print(f"[+] upload result: {res!r}", flush=True)
    if res not in ("", "0", None):
        print(f"[!] upload returned a code -> likely REJECTED: {res!r}", flush=True)
    print("[*] polling for telnet1 update-stage (telnet :23)...", flush=True)
    deadline = time.time() + 260
    t0 = time.time()
    while time.time() < deadline:
        t = port_open(ip, 23)
        p = port_open(ip, 80)
        dt = int(time.time() - t0)
        print(f"{dt}s: telnet={'open' if t else 'closed'} web={'open' if p else 'closed'}",
              flush=True)
        if t:
            print("[+] TELNET OPEN -- update-stage reached", flush=True)
            return
        time.sleep(5)
    print("[!] telnet did not open within window", flush=True)


if __name__ == "__main__":
    main()
