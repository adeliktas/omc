#!/usr/bin/env bash
# Build the OMC OmniRoute image: the pinned upstream release (UPSTREAM) plus patches/*.patch.
# usage: omniroute/build.sh <image-tag> [extra docker build args, e.g. --target builder]
# OMNIROUTE_UPSTREAM overrides the clone URL (e.g. a local mirror).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
tag=${1:?usage: build.sh <image-tag> [docker build args...]}
shift
{ read -r url; read -r ref; } < "$here/UPSTREAM"
src=$(mktemp -d)
trap 'rm -rf "$src"' EXIT
git clone -q --depth 1 --branch "$ref" "${OMNIROUTE_UPSTREAM:-$url}" "$src"
for p in "$here"/patches/*.patch; do git -C "$src" apply --binary "$p"; done
docker build -t "$tag" "$@" "$src"
