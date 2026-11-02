#!/bin/bash
# Build the AppImage from the release binary and resources.
set -e
cd "$(dirname "$0")/.."
. "$HOME/.cargo/env"
bash scripts/fetch-python.sh
(cd src-tauri && cargo clean -p goblin)
CARGO_INCREMENTAL=0 npm run tauri build -- --no-bundle

APPDIR=/tmp/Goblin.AppDir
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib/Goblin/_up_" \
  "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/128x128/apps"
cp src-tauri/target/release/goblin "$APPDIR/usr/bin/goblin"
rsync -a --exclude "__pycache__" Runtime Resources product.json scripts vendor \
  "$APPDIR/usr/lib/Goblin/_up_/"
cp src-tauri/icons/128x128.png "$APPDIR/goblin.png"
cp src-tauri/icons/128x128.png "$APPDIR/usr/share/icons/hicolor/128x128/apps/goblin.png"
printf "%s\n" "[Desktop Entry]" "Name=Goblin" "Exec=goblin" "Icon=goblin" \
  "Type=Application" "Categories=Utility;" "Terminal=false" > "$APPDIR/goblin.desktop"
cp "$APPDIR/goblin.desktop" "$APPDIR/usr/share/applications/goblin.desktop"
printf "%s\n" "#!/bin/sh" "HERE=\$(dirname \"\$(readlink -f \"\$0\")\")" \
  "export GOBLIN_ROOT=\"\$HERE/usr/lib/Goblin/_up_\"" \
  "exec \"\$HERE/usr/bin/goblin\" \"\$@\"" > "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"

TOOL="$HOME/.cache/goblin/appimagetool"
mkdir -p "$(dirname "$TOOL")"
if [ ! -x "$TOOL" ]; then
  curl -sSL -o "$TOOL" \
    https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
  chmod +x "$TOOL"
fi
VERSION=$(python3 -c "import json; print(json.load(open('product.json'))['version'])")
ARCH=x86_64 "$TOOL" --appimage-extract-and-run "$APPDIR" \
  "src-tauri/target/release/bundle/Goblin-${VERSION}-x86_64.AppImage" 2>&1 | tail -2
echo "AppImage built"
