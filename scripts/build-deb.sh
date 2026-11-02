#!/bin/bash
# Build the Debian package from a clean engine tree.
set -e
cd "$(dirname "$0")/.."
find Runtime -name __pycache__ -type d -prune -exec rm -rf {} +
npm run tauri build
