use std::fs;
use std::io::BufRead;
use std::io::BufReader;
use std::io::Read;
use std::io::Write;
use std::net::TcpListener;
use std::net::TcpStream;
use std::path::Path;
use std::thread;

const DISP_WIDTH: usize = 480;
const DISP_HEIGHT: usize = 240;
const WALLPAPER_BYTES: usize = DISP_WIDTH * DISP_HEIGHT * 2;

const RINGTONE_BLOCK: usize = 1000;
const RINGTONE_MAX_BYTES: usize = 480 * RINGTONE_BLOCK;

const MAX_BODY: usize = 4 * 1024 * 1024;

const INVALID_NAME_ERROR: &str =
    "{\"ok\":false,\"error\":\"name must be a plain file name not starting with '.'\"}";
const ACTIVE_NAME_ERROR: &str =
    "{\"ok\":false,\"error\":\"uploads may not replace the active file\"}";

const INDEX_HTML: &str = include_str!("../web/index.html");
const APP_JS: &str = include_str!("../web/app.js");

const WALLPAPERS_DIR: &str = "/tmp/telepwn_data/frontend/wallpapers";
const ACTIVE_WALLPAPER_NAME: &str = "active_wallpaper.rgb565";

const RINGTONES_DIR: &str = "/tmp/telepwn_data/frontend/ringtones";
const ACTIVE_RINGTONE_NAME: &str = "active_ringtone.pcm";

struct Request {
    method: String,
    path: String,
    query: String,
    body: Vec<u8>,
}

fn read_request(stream: &TcpStream) -> std::io::Result<Request> {
    let mut reader = BufReader::new(stream.try_clone()?);

    let mut request_line = String::new();
    reader.read_line(&mut request_line)?;
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or("").to_string();
    let target = parts.next().unwrap_or("");
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    let path = path.to_string();
    let query = query.to_string();

    let mut content_length = 0usize;
    loop {
        let mut header = String::new();
        let n = reader.read_line(&mut header)?;
        if n == 0 {
            break;
        }
        let trimmed = header.trim_end();
        if trimmed.is_empty() {
            break;
        }
        if let Some((name, value)) = trimmed.split_once(':')
            && name.trim().eq_ignore_ascii_case("content-length")
        {
            content_length = value.trim().parse().unwrap_or(0);
        }
    }

    if content_length > MAX_BODY {
        return Err(std::io::Error::other("body too large"));
    }

    let mut body = vec![0u8; content_length];
    reader.read_exact(&mut body)?;

    Ok(Request {
        method,
        path,
        query,
        body,
    })
}

fn respond(
    stream: &mut TcpStream,
    status: &str,
    content_type: &str,
    body: &[u8],
) -> std::io::Result<()> {
    let header = format!(
        "HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n",
        body.len()
    );
    stream.write_all(header.as_bytes())?;
    stream.write_all(body)?;
    stream.flush()
}

fn respond_json(stream: &mut TcpStream, status: &str, body: &str) -> std::io::Result<()> {
    respond(stream, status, "application/json", body.as_bytes())
}

fn query_param<'a>(query: &'a str, key: &str) -> Option<&'a str> {
    query
        .split('&')
        .filter_map(|pair| pair.split_once('='))
        .find(|(k, _)| *k == key)
        .map(|(_, v)| v)
}

fn safe_name(name: &str) -> bool {
    !name.is_empty() && !name.starts_with('.') && !name.contains('/')
}

fn store_atomic(dir: &str, name: &str, data: &[u8]) -> std::io::Result<()> {
    fs::create_dir_all(dir)?;
    let dir = Path::new(dir);
    let path = dir.join(name);
    let tmp = dir.join(format!(".{name}.upload.tmp"));
    {
        let mut file = fs::File::create(&tmp)?;
        file.write_all(data)?;
        file.sync_all()?;
    }
    fs::rename(&tmp, &path)
}

fn handle_upload(
    stream: &mut TcpStream,
    dir: &str,
    active_name: &str,
    query: &str,
    body: &[u8],
) -> std::io::Result<()> {
    let Some(name) = query_param(query, "name").filter(|n| safe_name(n)) else {
        return respond_json(stream, "400 Bad Request", INVALID_NAME_ERROR);
    };
    if name == active_name {
        return respond_json(stream, "400 Bad Request", ACTIVE_NAME_ERROR);
    }
    match store_atomic(dir, name, body) {
        Ok(()) => respond_json(
            stream,
            "200 OK",
            &format!("{{\"ok\":true,\"name\":\"{}\"}}", escape(name)),
        ),
        Err(e) => respond_json(
            stream,
            "500 Internal Server Error",
            &format!("{{\"ok\":false,\"error\":\"{}\"}}", escape(&e.to_string())),
        ),
    }
}

fn handle_list(stream: &mut TcpStream, dir: &str) -> std::io::Result<()> {
    let entries = match fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return respond_json(stream, "200 OK", "{\"ok\":true,\"files\":[]}");
        }
        Err(e) => {
            return respond_json(
                stream,
                "500 Internal Server Error",
                &format!("{{\"ok\":false,\"error\":\"{}\"}}", escape(&e.to_string())),
            );
        }
    };
    let mut names: Vec<String> = entries
        .filter_map(Result::ok)
        .filter(|entry| entry.file_type().is_ok_and(|t| t.is_file()))
        .filter_map(|entry| entry.file_name().into_string().ok())
        .filter(|name| safe_name(name))
        .collect();
    names.sort();
    let files = names
        .iter()
        .map(|name| format!("\"{}\"", escape(name)))
        .collect::<Vec<_>>()
        .join(",");
    respond_json(
        stream,
        "200 OK",
        &format!("{{\"ok\":true,\"files\":[{files}]}}"),
    )
}

fn handle_download(
    stream: &mut TcpStream,
    dir: &str,
    query: &str,
    default_name: &str,
) -> std::io::Result<()> {
    let name = query_param(query, "name").unwrap_or(default_name);
    if !safe_name(name) {
        return respond_json(stream, "400 Bad Request", INVALID_NAME_ERROR);
    }
    let path = Path::new(dir).join(name);
    if !fs::symlink_metadata(&path).is_ok_and(|m| m.is_file()) {
        return respond_json(
            stream,
            "404 Not Found",
            "{\"ok\":false,\"error\":\"no such file\"}",
        );
    }
    match fs::read(&path) {
        Ok(data) => respond(stream, "200 OK", "application/octet-stream", &data),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => respond_json(
            stream,
            "404 Not Found",
            "{\"ok\":false,\"error\":\"no such file\"}",
        ),
        Err(e) => respond_json(
            stream,
            "500 Internal Server Error",
            &format!("{{\"ok\":false,\"error\":\"{}\"}}", escape(&e.to_string())),
        ),
    }
}

fn escape(s: &str) -> String {
    s.replace('\\', "\\\\").replace('"', "\\\"")
}

fn handle(mut stream: TcpStream) {
    let request = match read_request(&stream) {
        Ok(r) => r,
        Err(_) => return,
    };

    let result = match (request.method.as_str(), request.path.as_str()) {
        ("GET", "/") | ("GET", "/index.html") => respond(
            &mut stream,
            "200 OK",
            "text/html; charset=utf-8",
            INDEX_HTML.as_bytes(),
        ),
        ("GET", "/app.js") => respond(
            &mut stream,
            "200 OK",
            "application/javascript; charset=utf-8",
            APP_JS.as_bytes(),
        ),
        ("GET", "/api/info") => respond_json(
            &mut stream,
            "200 OK",
            &format!(
                "{{\"width\":{DISP_WIDTH},\"height\":{DISP_HEIGHT},\"wallpaper_bytes\":{WALLPAPER_BYTES},\"ringtone_block\":{RINGTONE_BLOCK},\"ringtone_max_bytes\":{RINGTONE_MAX_BYTES},\"sample_rate\":8000}}"
            ),
        ),
        ("GET", "/api/wallpapers") => handle_list(&mut stream, WALLPAPERS_DIR),
        ("GET", "/api/ringtones") => handle_list(&mut stream, RINGTONES_DIR),
        ("GET", "/api/wallpaper") => handle_download(
            &mut stream,
            WALLPAPERS_DIR,
            &request.query,
            ACTIVE_WALLPAPER_NAME,
        ),
        ("GET", "/api/ringtone") => handle_download(
            &mut stream,
            RINGTONES_DIR,
            &request.query,
            ACTIVE_RINGTONE_NAME,
        ),
        ("POST", "/api/wallpaper") => handle_upload(
            &mut stream,
            WALLPAPERS_DIR,
            ACTIVE_WALLPAPER_NAME,
            &request.query,
            &request.body,
        ),
        ("POST", "/api/ringtone") => handle_upload(
            &mut stream,
            RINGTONES_DIR,
            ACTIVE_RINGTONE_NAME,
            &request.query,
            &request.body,
        ),
        _ => respond_json(
            &mut stream,
            "404 Not Found",
            "{\"ok\":false,\"error\":\"not found\"}",
        ),
    };

    let _ = result;
}

fn main() -> std::io::Result<()> {
    let port: u16 = std::env::var("PORT")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(80);
    let bind = std::env::var("BIND").unwrap_or_else(|_| String::from("0.0.0.0"));

    let listener = TcpListener::bind((bind.as_str(), port))?;
    eprintln!(
        "mediaup listening on {bind}:{port}\n  wallpapers -> {WALLPAPERS_DIR}\n  ringtones  -> {RINGTONES_DIR}"
    );

    for stream in listener.incoming() {
        let Ok(stream) = stream else { continue };
        thread::spawn(move || handle(stream));
    }

    Ok(())
}
