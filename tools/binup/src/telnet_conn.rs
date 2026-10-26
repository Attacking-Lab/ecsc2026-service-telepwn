use telnet::{Event, Telnet};

pub(crate) struct TelnetConn {
    conn: Telnet,
    pub(crate) verbose: bool,
}

impl TelnetConn {
    pub(crate) fn connect<R: AsRef<str>>(ip: R, port: u16, verbose: bool) -> Self {
        let conn =
            Telnet::connect((ip.as_ref(), port), 1).expect("No telnet connection to the phone");

        Self { conn, verbose }
    }

    pub(crate) fn read_until<R: AsRef<str>>(&mut self, needle: R) -> String {
        let mut ret = String::new();
        loop {
            let event = self.conn.read();

            if self.verbose {
                print!("DEBUG: {:?}", event);
            }

            if let Ok(Event::Data(d)) = event {
                let d_str = String::from_utf8_lossy(&d);
                if !d_str.is_ascii() {
                    if self.verbose {
                        println!();
                    }
                    continue;
                }

                ret.push_str(&d_str);

                if self.verbose {
                    println!("{}", ret);
                }

                if ret.ends_with(needle.as_ref()) {
                    break;
                }
            }
        }

        ret
    }

    pub(crate) fn read_line(&mut self) -> String {
        self.read_until("\n")
    }

    pub(crate) fn send_line_after<R: AsRef<str>>(&mut self, when: R, what: R) {
        self.read_until(when);

        let mut data = what.as_ref().to_string();
        if !data.ends_with("\n") {
            data.push('\n');
        }

        self.conn
            .write(data.as_bytes())
            .expect("Could not write data");
    }

    pub(crate) fn stream_until<R: AsRef<str>>(&mut self, what: R) {
        loop {
            let line = self.read_line();
            if line.contains(what.as_ref()) {
                println!("{}", line.split_once(what.as_ref()).unwrap().0);
                break;
            }
            print!("{}", line)
        }
    }
}
