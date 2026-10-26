# binup

This tool is used for uploading binaries to the phone.
They can be executed immediately after uploading as well for testing!

## Install

```bash
cargo install --path .
```

## Usage

```
Usage: binup [OPTIONS] <PATH> <IP> <PORT> <PHONE_IP> <PHONE_PORT>

Arguments:
  <PATH>        Path to the binary
  <IP>          IP the phone should connect to
  <PORT>        Port the phone should connect to
  <PHONE_IP>    IP of the phone
  <PHONE_PORT>  Port of the phone

Options:
  -r, --run   Immediately run the uploaded program
  -h, --help  Print help
```
