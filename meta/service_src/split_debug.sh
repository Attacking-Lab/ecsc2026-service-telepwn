#!/bin/sh
# Split DWARF debug info out of a release binary into a sidecar, strip the binary,
# then link them with a .gnu_debuglink section so gdb/lldb re-associate the two
# automatically (debugger looks for the sidecar next to the binary, in a ./.debug/
# subdir, or under the global debug dir, matched by name + CRC).
#
# Build the binaries with debuginfo first (service/Cargo.toml sets
# [profile.release] debug="full", strip=false), then run this on each one.
#
# Usage: ./split_debug.sh <binary> [<dbgsym_dir>]
#   <binary>      unstripped, debug=full ELF; stripped IN PLACE
#   <dbgsym_dir>  where to write <name>.debug  (default: <binary_dir>/dbgsym)
# Override the tool with OBJCOPY=arm-linux-gnueabi-objcopy if preferred.
set -eu

OBJCOPY="${OBJCOPY:-llvm-objcopy}"
bin="$1"
name=$(basename "$bin")
bindir=$(cd "$(dirname "$bin")" && pwd)
dbgdir="${2:-$bindir/dbgsym}"
mkdir -p "$dbgdir"
dbg="$dbgdir/$name.debug"

# 1. copy only the DWARF (+ symbol table) into the sidecar
"$OBJCOPY" --only-keep-debug "$bin" "$dbg"
chmod 0644 "$dbg"

# 2. strip the shipped binary completely (DWARF + .symtab)
"$OBJCOPY" --strip-all "$bin"

# 3. embed the sidecar's basename + CRC32 so a debugger can find it.
#    Run from the sidecar dir so only the basename is stored, never a path.
( cd "$dbgdir" && "$OBJCOPY" --add-gnu-debuglink="$name.debug" "$bin" )

echo "[+] $name"
echo "    stripped binary : $bin"
echo "    debug sidecar   : $dbg"
