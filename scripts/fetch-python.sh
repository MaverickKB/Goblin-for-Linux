#!/bin/bash
# Fetch the pinned CPython runtime used by the packaged app.
set -e
cd "$(dirname "$0")/.."
VERSION="3.12.16"
RELEASE="20261026"
ARCHIVE="cpython-${VERSION}+${RELEASE}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
URL="https://github.com/astral-sh/python-build-standalone/releases/download/${RELEASE}/${ARCHIVE//+/%2B}"
SHA256="ce4f6d0a6cfdb3bed6e44c2f9affd85943c76a42e25ad3a259002f31d6fb8a20"
TARGET="vendor/python"

if [ -x "$TARGET/bin/python3" ]; then
  if "$TARGET/bin/python3" -c "import sys; raise SystemExit(0 if sys.version_info[:3] == (3, 12, 16) else 1)"; then
    exit 0
  fi
fi

rm -rf "$TARGET"
mkdir -p vendor
TEMP=$(mktemp -d)
trap 'rm -rf "$TEMP"' EXIT
echo "Downloading CPython ${VERSION}"
curl -sSL -o "$TEMP/python.tar.gz" "$URL"
echo "$SHA256  $TEMP/python.tar.gz" | sha256sum -c -
tar -xzf "$TEMP/python.tar.gz" -C vendor
"$TARGET/bin/python3" --version
