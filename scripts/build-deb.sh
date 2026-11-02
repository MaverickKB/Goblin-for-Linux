#!/bin/bash
# Build the Debian package from a clean engine tree.
set -e
cd "$(dirname "$0")/.."
. "$HOME/.cargo/env"
find Runtime -name __pycache__ -type d -prune -exec rm -rf {} +
# Cargo can reuse an old frontend asset expansion after UI changes. Cleaning
# this crate forces the embedded bundle to match src/.
(cd src-tauri && cargo clean -p goblin)
CARGO_INCREMENTAL=0 npm run tauri build
