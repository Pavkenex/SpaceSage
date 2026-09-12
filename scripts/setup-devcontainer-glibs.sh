#!/usr/bin/env bash
# Install libEGL + libglvnd into /opt/data/syslibs for dev containers that
# lack system GL libraries (PySide6/Qt needs them even for offscreen use).
# No root required: packages are extracted locally, not installed system-wide.
# Idempotent. Only needed in minimal containers; normal machines skip it.
set -euo pipefail

DEST=/opt/data/syslibs
ARCH=$(dpkg --print-architecture 2>/dev/null || echo arm64)
BASE=http://deb.debian.org/debian/pool/main/libg/libglvnd

if find "$DEST" -name 'libEGL.so.1' 2>/dev/null | grep -q .; then
    echo "GL libs already present in $DEST — nothing to do."
    exit 0
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
cd "$TMP"
curl -s "$BASE/" -o listing.html
EGL=$(grep -oE "libegl1_[^\"]*_${ARCH}\.deb" listing.html | sort -V | tail -1)
GLV=$(grep -oE "libglvnd0_[^\"]*_${ARCH}\.deb" listing.html | sort -V | tail -1)
[ -n "$EGL" ] && [ -n "$GLV" ] || { echo "could not find packages for arch $ARCH" >&2; exit 1; }
curl -sO "$BASE/$EGL"
curl -sO "$BASE/$GLV"
mkdir -p "$DEST"
for f in ./*.deb; do dpkg-deb -x "$f" "$DEST"; done
echo "installed $EGL + $GLV -> $DEST"
