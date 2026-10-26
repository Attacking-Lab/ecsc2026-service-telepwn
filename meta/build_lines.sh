#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
tool_dir="$script_dir/audio_encode"

image=""

# In CI the encoder image is built and pushed by the build-gen-audio-image
# workflow step before this runs, so only pull it. Locally build it but never
# push: the staging VM builds run this with no registry login, and a local
# build must not replace CI's image.
ensure_image() {
    [ -n "$image" ] && return 0
    image="${GEN_AUDIO_IMAGE:-ecsc2026-service-telepwn/gen-audio-ci:latest}"
    if [ -n "${CI+x}" ]; then
        docker pull "$image"
    else
        docker build -t "$image" "$tool_dir"
    fi
}

# an outdir that already exists is skipped, so running both build hooks (or
# rerunning one) doesn't redo the encode; remove the directory to force it
encode() {
    input=$1
    outdir=$2
    if [ -d "$outdir" ]; then
        echo "$outdir exists, skipping encode of $input"
        return 0
    fi
    ensure_image
    mkdir -p "$outdir"
    docker run --rm --user "$(id -u):$(id -g)" \
        -v "$tool_dir:/tool:ro" -v "$outdir:/out" \
        "$image" python /tool/encode_audio.py "/tool/$input" /out
}

encode_service() {
    encode lines.json "$repo_root/service/backend/lines"
}

encode_checker() {
    encode lines.json "$repo_root/checker/lines"
}

case "${1:-all}" in
    service) encode_service ;;
    checker) encode_checker ;;
    all) encode_service; encode_checker ;;
    *) echo "usage: $0 [service|checker|all]" >&2; exit 1 ;;
esac
