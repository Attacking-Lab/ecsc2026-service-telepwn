{ pkgs ? import <nixpkgs> { } }:

let
  rustOverlay = import (builtins.fetchTarball {
    url = "https://github.com/oxalica/rust-overlay/archive/892c035d7c2ff75acd5da10424a47ab454e1f3dc.tar.gz";
    sha256 = "12c31q5mi3391yfjma808nhy6sliynyf68wviqch5ni0fv86m618";
  });

  rustPkgs = pkgs.extend rustOverlay;

  rustToolchain = rustPkgs.rust-bin.nightly.latest.default.override {
    extensions = [ "rust-src" ];
    targets = [ "arm-unknown-linux-musleabi" ];
  };

  hostTriple = pkgs.stdenv.hostPlatform.rust.rustcTarget;

  python = pkgs.python3;

  pythonEnv = python.withPackages (ps: [ ps.pwntools ]);

  armBinutils = pkgs.pkgsCross.armv7l-hf-multiplatform.buildPackages.binutils;

  armAlsaLib = pkgs.stdenv.mkDerivation {
    pname = "alsa-lib-arm-musleabi-static";
    inherit (pkgs.alsa-lib) version src;
    nativeBuildInputs = [ pkgs.zig ];
    dontStrip = true;
    dontFixup = true;
    configurePhase = ''
      export HOME=$TMPDIR
      export ZIG_GLOBAL_CACHE_DIR=$TMPDIR/zig-global ZIG_LOCAL_CACHE_DIR=$TMPDIR/zig-local
      export CC="zig cc -target arm-linux-musleabi -mcpu=arm1176jzf_s"
      export AR="zig ar" RANLIB="zig ranlib"
      export CFLAGS="-O2 -fno-sanitize=undefined"
      ./configure --host=arm-linux-musleabi --prefix=$out \
        --enable-static --disable-shared \
        --disable-ucm --disable-topology --disable-python \
        --with-libdl=no --with-versioned=no
    '';
    buildPhase = ''
      make -j$NIX_BUILD_CORES
    '';
    installPhase = ''
      make install
    '';
  };
in
pkgs.mkShell {
  packages = [
    pkgs.docker-client
    pkgs.docker-compose
    rustToolchain
    pythonEnv
    armBinutils
    pkgs.gdb
    pkgs.espeak-ng
  ];

  RUST_SRC_PATH = "${rustToolchain}/lib/rustlib/src/rust/library";

  LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [
    pkgs.stdenv.cc.cc.lib
    pkgs.zlib
  ];

  CARGO_TARGET_ARM_UNKNOWN_LINUX_MUSLEABI_LINKER = "rust-lld";
  CARGO_TARGET_ARM_UNKNOWN_LINUX_MUSLEABI_RUSTFLAGS = "-C linker-flavor=ld.lld";

  PKG_CONFIG_arm_unknown_linux_musleabi = "${pkgs.pkg-config-unwrapped}/bin/pkg-config";
  PKG_CONFIG_PATH_arm_unknown_linux_musleabi = "${armAlsaLib}/lib/pkgconfig";
  PKG_CONFIG_ALLOW_CROSS_arm_unknown_linux_musleabi = "1";

  shellHook = ''
    runtimeSock="''${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/docker.sock"
    if [ -z "$DOCKER_HOST" ] && [ ! -S /var/run/docker.sock ] && [ -S "$runtimeSock" ]; then
      export DOCKER_HOST="unix://$runtimeSock"
    fi
    unset runtimeSock

    export PATH="${rustToolchain}/lib/rustlib/${hostTriple}/bin:$PATH"

    export VENV_DIR="''${VENV_DIR:-$HOME/.virtualenvs/telepwn}"

    if [ ! -d "$VENV_DIR" ]; then
      echo "creating venv at $VENV_DIR"
      ${python}/bin/python -m venv "$VENV_DIR"
      "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    fi
    "$VENV_DIR/bin/pip" install --quiet numpy scipy json5 espeakng enochecker-test

    source "$VENV_DIR/bin/activate"

    export PYTHONPATH="${pythonEnv}/${python.sitePackages}''${PYTHONPATH:+:$PYTHONPATH}"

    export NIX_SHELL_PRESERVE_PROMPT=1
    PS1='\n\[\033[1;32m\][telepwn:\w]\$\[\033[0m\] '

    if ! docker info >/dev/null 2>&1; then
      echo "warning: no docker daemon reachable (set DOCKER_HOST or enable virtualisation.docker)"
    fi

    echo "venv: $VIRTUAL_ENV ($(python --version))"
  '';
}
