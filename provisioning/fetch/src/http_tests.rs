use super::*;

#[test]
fn url_with_port_and_path() {
    assert_eq!(
        parse_url("http://10.42.0.1:9003/fw/3.tar.gz"),
        Some(Url {
            host: "10.42.0.1".into(),
            port: 9003,
            path: "/fw/3.tar.gz".into(),
        })
    );
}

#[test]
fn url_defaults() {
    assert_eq!(
        parse_url("http://example"),
        Some(Url {
            host: "example".into(),
            port: 80,
            path: "/".into(),
        })
    );
}

#[test]
fn url_rejects_unsupported() {
    assert_eq!(parse_url("https://x/"), None);
    assert_eq!(parse_url("http://:80/"), None);
    assert_eq!(parse_url("http://u@x/"), None);
    assert_eq!(parse_url("http://x:port/"), None);
    assert_eq!(parse_url("http:///a"), None);
}

#[test]
fn request_line() {
    let url = parse_url("http://h:1/team").unwrap();
    assert_eq!(
        request(&url),
        "GET /team HTTP/1.0\r\nHost: h:1\r\nConnection: close\r\n\r\n"
    );
}

#[test]
fn head_end_found_only_when_complete() {
    assert_eq!(head_end(b"HTTP/1.0 200 OK\r\n"), None);
    assert_eq!(head_end(b"HTTP/1.0 200 OK\r\n\r\nbody"), Some(19));
}

#[test]
fn head_with_length() {
    let h = parse_head(b"HTTP/1.0 200 OK\r\nServer: x\r\ncontent-length: 42\r\n\r\n");
    assert_eq!(
        h,
        Some(Head {
            status: 200,
            content_length: Some(42),
        })
    );
}

#[test]
fn head_without_length() {
    let h = parse_head(b"HTTP/1.1 404 Not Found\r\n\r\n");
    assert_eq!(
        h,
        Some(Head {
            status: 404,
            content_length: None,
        })
    );
}

#[test]
fn head_rejects_garbage() {
    assert_eq!(parse_head(b"SSH-2.0-OpenSSH\r\n\r\n"), None);
    assert_eq!(parse_head(b"HTTP/1.0 abc\r\n\r\n"), None);
    assert_eq!(
        parse_head(b"HTTP/1.0 200 OK\r\nContent-Length: x\r\n\r\n"),
        None
    );
}
