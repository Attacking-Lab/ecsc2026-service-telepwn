//! Minimal HTTP/1.0 request building and response-head parsing.

/// A parsed `http://host[:port]/path` URL.
#[derive(Debug, PartialEq, Eq)]
pub struct Url {
    pub host: String,
    pub port: u16,
    pub path: String,
}

/// The parts of a response head that the client acts on.
#[derive(Debug, PartialEq, Eq)]
pub struct Head {
    pub status: u16,
    pub content_length: Option<u64>,
}

/// Parses a plain-HTTP URL; `https` and userinfo are not supported.
pub fn parse_url(url: &str) -> Option<Url> {
    let rest = url.strip_prefix("http://")?;
    let (authority, path) = match rest.find('/') {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, "/"),
    };
    if authority.is_empty() || authority.contains('@') {
        return None;
    }
    let (host, port) = match authority.rsplit_once(':') {
        Some((h, p)) => (h, p.parse().ok()?),
        None => (authority, 80),
    };
    if host.is_empty() {
        return None;
    }
    Some(Url {
        host: host.to_string(),
        port,
        path: path.to_string(),
    })
}

/// Builds the GET request for `url`.
pub fn request(url: &Url) -> String {
    format!(
        "GET {} HTTP/1.0\r\nHost: {}:{}\r\nConnection: close\r\n\r\n",
        url.path, url.host, url.port
    )
}

/// Returns the length of the head including the blank line, once `buf` holds all of it.
pub fn head_end(buf: &[u8]) -> Option<usize> {
    buf.windows(4).position(|w| w == b"\r\n\r\n").map(|i| i + 4)
}

/// Parses a response head (status line plus headers).
pub fn parse_head(head: &[u8]) -> Option<Head> {
    let text = std::str::from_utf8(head).ok()?;
    let mut lines = text.split("\r\n");
    let mut status_line = lines.next()?.split_whitespace();
    if !status_line.next()?.starts_with("HTTP/1.") {
        return None;
    }
    let status = status_line.next()?.parse().ok()?;
    let mut content_length = None;
    for line in lines {
        if let Some((name, value)) = line.split_once(':')
            && name.trim().eq_ignore_ascii_case("content-length")
        {
            content_length = Some(value.trim().parse().ok()?);
        }
    }
    Some(Head {
        status,
        content_length,
    })
}

#[cfg(test)]
#[path = "http_tests.rs"]
mod tests;
