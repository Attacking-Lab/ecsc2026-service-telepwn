#!/usr/bin/env python3
"""Build a NOTICE file from `cargo tree`.

Enumerate every third-party Rust dependency of the given crate directories (default: all
crates found in the repo), dedupe them by name+version, resolve each one's license, and
emit a NOTICE listing every dependency with its SPDX license followed by the full license
text pulled from the crate source under ~/.cargo/registry/src.

The dependency set comes from `cargo tree --prefix none --format '{p}|{l}'` run in each
crate dir, honouring that crate's own .cargo/config target. First-party path crates (shown
by cargo tree with an absolute path in parens) are skipped. Edge kinds default to "normal"
(the code linked into the binary); pass -e normal,build to also include build deps.

    python3 meta/gen_notice.py -o NOTICE
    python3 meta/gen_notice.py service/backend tools/mediaup -o NOTICE
    python3 meta/gen_notice.py --no-text        # dependency+license list only
"""
import argparse
import glob
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PKG_RE = re.compile(r"^(?P<name>\S+) v(?P<version>\S+?)(?: \((?P<extra>.*)\))?$")

LICENSE_GLOBS = ("LICENSE*", "LICENCE*", "COPYING*", "COPYRIGHT*", "NOTICE*", "UNLICENSE*")

ASSET_NOTICES = [
    (
        "National anthem audio - nationalanthems.info",
        "The default ringtone audio (national anthem recordings) is sourced from\n"
        "nationalanthems.info (https://nationalanthems.info/), used under the Creative\n"
        "Commons Attribution 4.0 International License (CC BY 4.0,\n"
        "https://creativecommons.org/licenses/by/4.0/).\n"
        "\n"
        "Per the site's terms of use (https://nationalanthems.info/faq.html#use),\n"
        "nationalanthems.info is credited as the source. The national anthems themselves\n"
        "are in the public domain; the individual recordings and arrangements are provided\n"
        "under CC BY 4.0. Files the site marks with a separate copyright notice are not\n"
        "included.",
    ),
]


def find_crates(root):
    crates = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "target" and not d.startswith(".")]
        if "Cargo.toml" in filenames:
            crates.append(dirpath)
    return sorted(crates)


def cargo_tree(crate_dir, edges, target):
    cmd = ["cargo", "tree", "--prefix", "none", "--format", "{p}|{l}", "-e", edges]
    if target:
        cmd += ["--target", target]
    res = subprocess.run(cmd, cwd=crate_dir, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"warning: cargo tree failed in {crate_dir}:\n{res.stderr.strip()}",
              file=sys.stderr)
        return []
    return res.stdout.splitlines()


def parse_deps(lines, deps):
    for line in lines:
        line = line.strip()
        if line.endswith(" (*)"):
            line = line[:-4]
        spec, sep, lic = line.rpartition("|")
        if not sep:
            continue
        m = PKG_RE.match(spec.strip())
        if not m:
            continue
        if m.group("extra") and m.group("extra").startswith("/"):
            continue
        deps[(m.group("name"), m.group("version"))] = lic.strip()


def license_texts(name, version):
    texts = []
    bases = glob.glob(os.path.expanduser(f"~/.cargo/registry/src/*/{name}-{version}"))
    if bases:
        seen = set()
        for pat in LICENSE_GLOBS:
            for path in sorted(glob.glob(os.path.join(bases[0], pat))):
                if os.path.isfile(path) and path not in seen:
                    seen.add(path)
                    with open(path, encoding="utf-8", errors="replace") as f:
                        texts.append((os.path.basename(path), f.read().strip()))
    return texts


def render(deps, with_text):
    out = ["NOTICE", ""]
    out.append("This product includes the following third-party Rust crates, each "
               "distributed under the license noted with it.")
    out.append("")
    for name, version in sorted(deps):
        out.append(f"  {name} {version} - {deps[(name, version)] or '(license not declared)'}")
    out.append("")
    if with_text:
        for name, version in sorted(deps):
            out.append("=" * 78)
            out.append(f"{name} {version} - {deps[(name, version)] or 'see below'}")
            out.append("=" * 78)
            texts = license_texts(name, version)
            if texts:
                for fname, text in texts:
                    out.append(f"----- {fname} -----")
                    out.append(text)
                    out.append("")
            else:
                out.append("(no license file shipped in the crate source; refer to the "
                           "SPDX identifier above and the crate's repository)")
                out.append("")
    if ASSET_NOTICES:
        out.append("")
        out.append("This product also includes the following non-code third-party content.")
        out.append("")
        for title, text in ASSET_NOTICES:
            out.append("=" * 78)
            out.append(title)
            out.append("=" * 78)
            out.append(text)
            out.append("")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description="build a NOTICE file from cargo tree")
    ap.add_argument("crates", nargs="*", help="crate dirs (default: all crates in the repo)")
    ap.add_argument("-e", "--edges", default="normal",
                    help="cargo tree edge kinds (default: normal; excludes dev/build deps)")
    ap.add_argument("--target", help="resolve dependencies for this target triple")
    ap.add_argument("-o", "--output", default="-", help="output file (default: stdout)")
    ap.add_argument("--no-text", action="store_true",
                    help="emit the dependency+license list only, no full license texts")
    args = ap.parse_args()

    crate_dirs = args.crates or find_crates(REPO)
    deps = {}
    for d in crate_dirs:
        parse_deps(cargo_tree(d, args.edges, args.target), deps)

    data = render(deps, not args.no_text)
    if args.output == "-":
        sys.stdout.write(data)
    else:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(data)
    print(f"[notice] {len(deps)} unique dependencies from {len(crate_dirs)} crate(s)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
