#!/usr/bin/env python3
"""Yealink T4xS web-interface client: login + firmware upload.

Reimplements the phone's browser-side login crypto (see js/common/commonjs.js):
  - GET loginForm sets a JSESSIONID cookie and returns a per-session RSA public key
    (g_rsa_n / g_rsa_e).
  - A random AES-128 key and IV are generated as md5(random) hex strings.
  - pwd   = base64( AES-CBC/ZeroPadding(password, key, iv) )
  - rsakey= hex( RSA-PKCS1v1.5( <32-char hex key string> ) )
  - rsaiv = hex( RSA-PKCS1v1.5( <32-char hex iv  string> ) )
  - POST login with username/pwd/rsakey/rsaiv (form-urlencoded), keeping the cookie.

Firmware upload (js/pages/page_settings-upgrade.js):
  - POST beforeupgrade  -> expect body "0"
  - POST upgrade&native=true  -> multipart, file field name "btnUpgrade",
    plus maxlength = RSAEncrypt("<JSESSIONID>;<maxlen>")
  - poll askUpgrade&checkupload=true -> FWUP_ERR code (0 == success)
"""

import argparse
import base64
import hashlib
import os
import re
import sys
import time

import requests

try:  # pypi pycryptodome
    from Crypto.Cipher import AES
    from Crypto.Cipher import PKCS1_v1_5
    from Crypto.PublicKey import RSA
except ImportError:  # debian python3-pycryptodome ships the Cryptodome namespace
    from Cryptodome.Cipher import AES
    from Cryptodome.Cipher import PKCS1_v1_5
    from Cryptodome.PublicKey import RSA

FWUP_ERR = {
    0: "Success", -1: "IO failed", -2: "Abnormal memory", -3: "Logging failed",
    -4: "URL invalid", -5: "ROM invalid", -6: "Device invalid",
    -7: "ROM version same", -8: "OEM version different", -9: "Tool invalid",
    -10: "Magic invalid", -11: "ROM head format invalid", -12: "ROM head verify failed",
    -13: "ROM block verify failed", -14: "ROM block data invalid", -15: "Calling overrun",
    -16: "ROM version low", -96: "Get Real ROM Version Error", -97: "Upgrade Create Error",
    -98: "Data Stream Error", -99: "Not Upgrade Error", -100: "Network Error",
}


_RES_RE = re.compile(r'<div id="_RES_INFO_"[^>]*>(.*?)</div>', re.S)


def _res_info(text: str) -> str:
    """Extract the phone's <div id="_RES_INFO_"> payload (what the browser's
    getresultinfo() reads as the ajax result)."""
    m = _RES_RE.search(text)
    return m.group(1).strip() if m else text.strip()


def _rand_md5_hex():
    return hashlib.md5(os.urandom(16)).hexdigest()  # 32 hex chars == 16 bytes


def _pkcs1v15_encrypt(msg: bytes, n: int, e: int) -> str:
    """RSA PKCS#1 v1.5 (type 2) public encryption, matching jsbn RSAKey.encrypt.
    Returns lowercase hex, zero-padded to the modulus byte length."""
    k = (n.bit_length() + 7) // 8
    key = RSA.construct((n, e))
    ct = PKCS1_v1_5.new(key).encrypt(msg)
    return ct.hex().rjust(k * 2, "0")


class YealinkWeb:
    def __init__(self, host, username="admin", password="admin", timeout=15,
                 verbose=True, scheme="auto"):
        self.host = host
        self.username = username
        self.password = password
        self.timeout = timeout
        self.verbose = verbose
        self.s = requests.Session()
        self.s.verify = False  # phone uses a self-signed cert on newer firmware
        import urllib3
        urllib3.disable_warnings()
        self.rsa_n = None
        self.rsa_e = None
        self.base = self._resolve_scheme(scheme)

    def _resolve_scheme(self, scheme):
        # Newer firmware (>= .85) forces HTTPS; older (.84) serves plain HTTP.
        if scheme in ("http", "https"):
            return f"{scheme}://{self.host}"
        try:
            r = self.s.get(f"http://{self.host}/", timeout=self.timeout,
                           allow_redirects=False)
            loc = r.headers.get("Location", "")
            if r.status_code in (301, 302) and loc.startswith("https"):
                self._log("[*] phone forces HTTPS")
                return f"https://{self.host}"
        except requests.RequestException:
            pass
        return f"http://{self.host}"

    def _log(self, *a):
        if self.verbose:
            print(*a, file=sys.stderr, flush=True)

    def _loginform_url(self):
        return (f"{self.base}/servlet?m=mod_listener&p=login&q=loginForm"
                f"&Random={os.urandom(4).hex()}")

    def _fetch_rsa_key(self):
        # Fresh loginForm sets JSESSIONID and hands out the session RSA pubkey.
        r = self.s.get(self._loginform_url(), timeout=self.timeout)
        r.raise_for_status()
        n = re.search(r'g_rsa_n\s*=\s*"([0-9A-Fa-f]+)"', r.text)
        # Newer firmware (>= .85) serves an empty form over HTTP and the real one only
        # over HTTPS. If the key isn't here and we're on HTTP, flip the base to HTTPS.
        if not n and self.base.startswith("http://"):
            self.base = f"https://{self.host}"
            self._log("[*] no RSA key over HTTP -> switching base to HTTPS")
            r = self.s.get(self._loginform_url(), timeout=self.timeout)
            r.raise_for_status()
            n = re.search(r'g_rsa_n\s*=\s*"([0-9A-Fa-f]+)"', r.text)
        e = re.search(r'g_rsa_e\s*=\s*"([0-9A-Fa-f]+)"', r.text)
        if not (n and e):
            raise RuntimeError("could not find g_rsa_n/g_rsa_e in login form")
        self.rsa_n = int(n.group(1), 16)
        self.rsa_e = int(e.group(1), 16)
        self._log(f"[*] session RSA modulus {self.rsa_n.bit_length()}-bit, "
                  f"e=0x{self.rsa_e:x}, cookie={self.s.cookies.get_dict()}")

    def login(self):
        self._fetch_rsa_key()
        key_hex = _rand_md5_hex()
        iv_hex = _rand_md5_hex()
        aes = AES.new(bytes.fromhex(key_hex), AES.MODE_CBC, bytes.fromhex(iv_hex))
        # YLForm.Encrypt encrypts "<random>;<JSESSIONID>;<password>", not the bare
        # password -- the server checks the embedded session id.
        # CRITICAL: the plaintext must NOT be an exact multiple of the AES block size.
        # CryptoJS ZeroPadding adds nothing when already aligned, and the phone then
        # rejects the login (errorno -3). The phone's own JS uses a long Math.random()
        # prefix; we mimic it and extend the random field until the plaintext is
        # unaligned, so ZeroPadding always appends real padding.
        sid = self.s.cookies.get("JSESSIONID", "")
        rnd = "0." + "".join(str(b % 10) for b in os.urandom(16))
        while len(f"{rnd};{sid};{self.password}".encode()) % 16 == 0:
            rnd += str(os.urandom(1)[0] % 10)
        plaintext = f"{rnd};{sid};{self.password}".encode()
        plaintext += b"\x00" * ((16 - len(plaintext) % 16) % 16)  # ZeroPadding
        pwd = base64.b64encode(aes.encrypt(plaintext)).decode()
        data = {
            "username": self.username,
            "pwd": pwd,
            "rsakey": _pkcs1v15_encrypt(key_hex.encode(), self.rsa_n, self.rsa_e),
            "rsaiv": _pkcs1v15_encrypt(iv_hex.encode(), self.rsa_n, self.rsa_e),
        }
        url = f"{self.base}/servlet?m=mod_listener&p=login&q=login"
        r = self.s.post(url, data=data, timeout=self.timeout)
        r.raise_for_status()
        body = r.text
        self._log(f"[*] login response: {body[:200]}")
        # authstatus: done | error | lock  (see AUTH_TYPE in commonjs.js)
        if '"lock"' in body:
            lt = re.search(r'"locktime"\s*:\s*"?(\d+)', body)
            raise RuntimeError(f"login LOCKED for {lt.group(1) if lt else '?'} min "
                               "(too many failed attempts)")
        if '"error"' in body:
            raise RuntimeError(f"login rejected (wrong credentials?): {body[:120]!r}")
        if '"done"' not in body and not self._probe_authenticated():
            raise RuntimeError(f"login failed, unexpected response: {body[:200]!r}")
        self._log("[+] authenticated")
        return True

    def _probe_authenticated(self):
        # The upgrade page 302-redirects to the login form when unauthenticated.
        url = f"{self.base}/servlet?m=mod_data&p=settings-upgrade&q=load"
        r = self.s.get(url, timeout=self.timeout, allow_redirects=False)
        return r.status_code == 200 and "loginForm" not in r.headers.get("Location", "")

    def _jsessionid(self):
        return self.s.cookies.get("JSESSIONID", "")

    def _rsa_encrypt_sessval(self, value: str) -> str:
        # YLForm.RSAEncrypt: RSA( "<JSESSIONID>;<value>" )
        payload = f"{self._jsessionid()};{value}".encode()
        return _pkcs1v15_encrypt(payload, self.rsa_n, self.rsa_e)

    def _get_upgrade_token(self):
        # g_strToken is embedded in the upgrade page and must accompany beforeupgrade.
        r = self.s.get(f"{self.base}/servlet?m=mod_data&p=settings-upgrade&q=load",
                       timeout=self.timeout)
        m = re.search(r'g_strToken\s*=\s*"([^"]+)"', r.text)
        if not m:
            raise RuntimeError("could not read g_strToken from upgrade page (not authed?)")
        return m.group(1)

    def upload_firmware(self, rom_path, poll=True, poll_timeout=600):
        size = os.path.getsize(rom_path)
        token = self._get_upgrade_token()
        self._log(f"[*] upgrade token = {token}")
        self._log(f"[*] beforeupgrade ...")
        r = self.s.post(f"{self.base}/servlet?m=mod_upgrade&q=beforeupgrade",
                        data={"token": token}, timeout=self.timeout)
        res = _res_info(r.text)
        self._log(f"[*] beforeupgrade -> res={res!r}")
        # JS treats 0=="" as success; empty or "0" == go ahead.
        if res not in ("", "0"):
            raise RuntimeError(f"beforeupgrade refused: res={res!r}")

        maxlen = self._rsa_encrypt_sessval(str(size))
        url = f"{self.base}/servlet?m=mod_upgrade&q=upgrade&native=true&maxlength={requests.utils.quote(maxlen)}"
        self._log(f"[*] uploading {rom_path} ({size} bytes) -> {url[:80]}...")
        # The phone expects a single multipart field named "UploadName".
        try:
            with open(rom_path, "rb") as f:
                files = {"UploadName": (os.path.basename(rom_path), f, "application/octet-stream")}
                r = self.s.post(url, files=files, timeout=max(self.timeout, 300))
            res = _res_info(r.text)
            self._log(f"[*] upload POST -> HTTP {r.status_code}, res={res!r}")
        except (requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout) as ex:
            # A clean accept often ends with the phone never answering: it either drops
            # the socket (ConnectionError) or simply stops responding while it reboots
            # into the update stage (ReadTimeout). Neither is a failure -- and treating
            # ReadTimeout as fatal skips the poll entirely and misses the telnet window.
            self._log(f"[*] upload POST: no response ({ex.__class__.__name__}) "
                      "-- likely flashing/rebooting; polling to confirm")
            res = ""

        if not poll:
            return res
        return self._poll_upgrade(poll_timeout)

    def _poll_upgrade(self, timeout):
        url = f"{self.base}/servlet?m=mod_upgrade&p=settings-upgrade&q=askUpgrade&checkupload=true"
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                r = self.s.get(url, timeout=self.timeout)
                txt = _res_info(r.text)
                if txt != last:
                    try:
                        code = int(txt)
                        self._log(f"[*] askUpgrade -> {code} ({FWUP_ERR.get(code, '?')})")
                    except ValueError:
                        self._log(f"[*] askUpgrade -> {txt!r}")
                    last = txt
            except requests.RequestException as ex:
                self._log(f"[*] askUpgrade (phone likely rebooting): {ex}")
                return last
            time.sleep(3)
        return last


def main():
    ap = argparse.ArgumentParser(description="Yealink T4xS web login / firmware upload")
    ap.add_argument("host")
    ap.add_argument("-u", "--username", default="admin")
    ap.add_argument("-p", "--password", default="admin")
    ap.add_argument("--upload", metavar="ROM", help="firmware .rom to upload after login")
    ap.add_argument("--no-poll", action="store_true", help="don't poll upgrade status")
    ap.add_argument("--login-only", action="store_true", help="just verify login")
    ap.add_argument("--timeout", type=int, default=15,
                    help="per-request timeout; beforeupgrade can take well over the "
                         "15s default on some firmware")
    args = ap.parse_args()

    w = YealinkWeb(args.host, args.username, args.password, timeout=args.timeout)
    w.login()
    print(f"[+] login OK on {args.host}")
    if args.upload:
        res = w.upload_firmware(args.upload, poll=not args.no_poll)
        print(f"[+] upload result: {res}")


if __name__ == "__main__":
    main()
