//! `fetch URL OUT|- [SHA256]` — tiny HTTP/1.0 GET client for the phone's stage 2.
//!
//! Only a 200 response is accepted. The body is checked against Content-Length and,
//! when given, a hex sha256; a file is written to `OUT.part` and renamed into place
//! only after every check passed. `-` writes the body to stdout instead.

mod http;

use std::env;
use std::fs;
use std::fs::File;
use std::io::Read;
use std::io::Write;
use std::net::SocketAddr;
use std::net::TcpStream;
use std::net::ToSocketAddrs;
use std::process::exit;
use std::time::Duration;

use sha2::Digest;
use sha2::Sha256;

const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);
const IO_TIMEOUT: Duration = Duration::from_secs(30);
const MAX_HEAD: usize = 16 * 1024;

type Result<T> = std::result::Result<T, String>;

fn resolve(host: &str, port: u16) -> Result<SocketAddr> {
    (host, port)
        .to_socket_addrs()
        .map_err(|e| format!("resolve {host}: {e}"))?
        .next()
        .ok_or_else(|| format!("resolve {host}: no address"))
}

fn read_head(s: &mut TcpStream) -> Result<(http::Head, Vec<u8>)> {
    let mut buf = Vec::new();
    let mut chunk = [0u8; 2048];
    loop {
        if let Some(end) = http::head_end(&buf) {
            let head = http::parse_head(&buf[..end]).ok_or("malformed response head")?;
            return Ok((head, buf.split_off(end)));
        }
        if buf.len() > MAX_HEAD {
            return Err("response head too large".into());
        }
        let n = s.read(&mut chunk).map_err(|e| format!("read: {e}"))?;
        if n == 0 {
            return Err("connection closed before end of head".into());
        }
        buf.extend_from_slice(&chunk[..n]);
    }
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn fetch(url: &str, out: &mut dyn Write, want_sha: Option<&str>) -> Result<u64> {
    let url = http::parse_url(url).ok_or("bad url (want http://host[:port]/path)")?;
    let addr = resolve(&url.host, url.port)?;
    let mut s = TcpStream::connect_timeout(&addr, CONNECT_TIMEOUT)
        .map_err(|e| format!("connect {addr}: {e}"))?;
    s.set_read_timeout(Some(IO_TIMEOUT))
        .and_then(|_| s.set_write_timeout(Some(IO_TIMEOUT)))
        .map_err(|e| format!("socket: {e}"))?;
    s.write_all(http::request(&url).as_bytes())
        .map_err(|e| format!("send: {e}"))?;

    let (head, first) = read_head(&mut s)?;
    if head.status != 200 {
        return Err(format!("http status {}", head.status));
    }

    let mut sha = Sha256::new();
    let mut total = 0u64;
    let mut sink = |data: &[u8]| -> Result<()> {
        sha.update(data);
        total += data.len() as u64;
        out.write_all(data).map_err(|e| format!("write: {e}"))
    };
    sink(&first)?;
    let mut chunk = vec![0u8; 16 * 1024];
    loop {
        let n = s.read(&mut chunk).map_err(|e| format!("read: {e}"))?;
        if n == 0 {
            break;
        }
        sink(&chunk[..n])?;
    }
    out.flush().map_err(|e| format!("write: {e}"))?;

    if let Some(len) = head.content_length
        && len != total
    {
        return Err(format!("short body: {total} of {len} bytes"));
    }
    if let Some(want) = want_sha {
        let got = hex(&sha.finalize());
        if !got.eq_ignore_ascii_case(want.trim()) {
            return Err(format!("sha256 mismatch: got {got}"));
        }
    }
    Ok(total)
}

fn run(args: &[String]) -> Result<()> {
    let (url, dest, sha) = match args {
        [url, dest] => (url, dest, None),
        [url, dest, sha] => (url, dest, Some(sha.as_str())),
        _ => return Err("usage: fetch URL OUT|- [SHA256]".into()),
    };
    if dest == "-" {
        fetch(url, &mut std::io::stdout().lock(), sha)?;
        return Ok(());
    }
    let part = format!("{dest}.part");
    let result = File::create(&part)
        .map_err(|e| format!("create {part}: {e}"))
        .and_then(|mut f| {
            let n = fetch(url, &mut f, sha)?;
            f.sync_all().map_err(|e| format!("sync {part}: {e}"))?;
            Ok(n)
        })
        .and_then(|n| {
            fs::rename(&part, dest).map_err(|e| format!("rename {part}: {e}"))?;
            Ok(n)
        });
    match result {
        Ok(n) => {
            eprintln!("fetch: {n} bytes -> {dest}");
            Ok(())
        }
        Err(e) => {
            let _ = fs::remove_file(&part);
            Err(e)
        }
    }
}

fn main() {
    let args: Vec<String> = env::args().skip(1).collect();
    if let Err(e) = run(&args) {
        eprintln!("fetch: {e}");
        exit(1);
    }
}
