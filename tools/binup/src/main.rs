use std::{io::Write, net::TcpListener};

use rand::random;

use clap::Parser;

mod telnet_conn;
use telnet_conn::TelnetConn;

#[derive(Debug, Parser)]
struct Args {
    #[arg(help = "Path to the binary")]
    path: std::path::PathBuf,

    #[arg(help = "IP the phone should connect to")]
    ip: String,

    #[arg(help = "Port the phone should connect to")]
    port: u16,

    #[arg(help = "IP of the phone")]
    phone_ip: String,

    #[arg(help = "Port of the phone")]
    phone_port: u16,

    #[arg(
        short,
        long,
        help = "Immediately run the uploaded program",
        default_value_t = false
    )]
    run: bool,
}

fn main() {
    let args = Args::parse();

    if !args.path.is_file() {
        println!("Can only upload files");
        return;
    }

    let path_clone = args.path.clone();

    let upload = std::thread::spawn(move || {
        let file = std::fs::read(path_clone).expect("Could not read binary");
        let listener =
            TcpListener::bind(("0.0.0.0", args.port)).expect("Could not bind local listener");
        if let Ok((mut conn, _)) = listener.accept() {
            conn.write_all(file.as_slice())
                .expect("Could not write the entire file");
            conn.flush().expect("Could not flush tcp conn");
            conn.shutdown(std::net::Shutdown::Both)
                .expect("Could not shut down socket");
        }
    });

    let mut conn = TelnetConn::connect(args.phone_ip, args.phone_port, false);
    let bin = args
        .path
        .file_name()
        .expect("Should have been a file")
        .to_string_lossy();

    conn.send_line_after(
        "#",
        &format!(
            "cd /tmp/udisk1/; busybox nc {} {} > {bin}; chmod u+x {bin}",
            args.ip, args.port,
        ),
    );

    upload.join().expect("Upload thread panicked");

    println!("[+] Uploaded {bin}");

    if args.run {
        println!("[+] Executing ./{bin}\n");
        let marker = format!("__BINUP_MARKER_{}__", random::<u32>());

        conn.send_line_after("#", &format!("./{bin}; echo {marker}"));
        conn.read_line();
        conn.stream_until(marker);
    } else {
        conn.read_until("#");
    }
}
