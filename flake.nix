{
  description = "telepwn cross-compilation toolchain for the Yealink SIP-T46S (armv6l)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    fenix = {
      url = "github:nix-community/fenix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = {
    self,
    nixpkgs,
    fenix,
    flake-utils,
  }:
    flake-utils.lib.eachDefaultSystem (
      system: let
        pkgs = import nixpkgs {inherit system;};
        fx = fenix.packages.${system};

        # The phone is armv6l running Linux 2.6.27. A fully static musl binary
        # links against no runtime libc, so it does not care about the phone's
        # ancient toolchain. Soft-float ABI (musleabi, not musleabihf) is the
        # portable choice and matches every tool's .cargo/config.toml.
        target = "arm-unknown-linux-musleabi";

        toolchain = fx.combine [
          fx.stable.rustc
          fx.stable.cargo
          fx.stable.clippy
          fx.stable.rustfmt
          fx.stable.rust-src
          fx.targets.${target}.stable.rust-std
        ];

        # `.cargo/config.toml` sets `linker = "rust-lld"`, a bare name cargo
        # resolves on PATH. fenix ships rust-lld inside the rustc sysroot rather
        # than on PATH, so expose it under that name.
        rustLld = pkgs.runCommand "rust-lld-shim" {} ''
          mkdir -p $out/bin
          ln -s ${toolchain}/lib/rustlib/${pkgs.stdenv.hostPlatform.config}/bin/rust-lld $out/bin/rust-lld
        '';
      in {
        devShells.default = pkgs.mkShell {
          name = "telepwn-arm";
          packages = [
            toolchain
            rustLld
            pkgs.lld
          ];
          shellHook = ''
            echo "telepwn ARM cross shell — target ${target}"
            echo "  build a tool:  (cd tools/lcdfb && cargo build --release)"
            echo "  artifact:      tools/lcdfb/target/${target}/release/lcdfb"
          '';
        };

        formatter = pkgs.alejandra;
      }
    );
}
