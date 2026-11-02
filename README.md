# Goblin for Linux

A native Linux agent companion built with Tauri 2 and the Goblin Python engine. Same product contract as the Mac app: one continuing conversation per Goblin profile, local tools, embedded AetherMind continuity, and an autonomy floor whose on switch belongs to the operator.

This port is in progress. The engine is vendored from Goblin for Mac 0.2.0. The Linux front end, audio, and packaging are being built here.

## Architecture

- `Runtime/` is the Goblin Python engine. It speaks NDJSON over stdin and stdout, one frame per line, exactly as it does on the Mac.
- `Resources/` holds the manuals, seed pack, and character art.
- `src-tauri/` is the Tauri shell. It spawns the engine, owns the window, audio, notifications, and tray.
- `src/` is the web UI.
- `scripts/goblin-credentials` is the Linux credential helper. It speaks the same JSON protocol as the Mac helper and stores secrets in the desktop keyring through `secret-tool`.

## Development

Requirements: Ubuntu 24.04 or later, Rust 1.77 or later, Node 20 or later, Python 3.11 or later, and the Tauri Linux dependencies (`libwebkit2gtk-4.1-dev`, `libgtk-3-dev`, `librsvg2-dev`, `libayatana-appindicator3-dev`, `libasound2-dev`, `libsecret-1-dev`, `libsecret-tools`).

Run the engine tests:

```sh
python3 -m unittest discover -s Tests/Python
```

Run the engine by hand:

```sh
GOBLIN_KEYCHAIN_HELPER="$PWD/scripts/goblin-credentials" \
GOBLIN_RESOURCES="$PWD/Resources" \
python3 Runtime/goblin_bridge.py --home "$HOME/.local/share/goblin" --manifest product.json
```

Build the desktop app:

```sh
npm install
npm run tauri dev
```

## Status

- [x] Engine vendored and running on Linux Python
- [x] Linux credential helper
- [ ] Tauri shell, chat, composer, attachments
- [ ] Settings: connections, autonomy, continuity, tools
- [ ] Animated avatars
- [ ] Voice
- [ ] Accounts, notifications, tray
- [ ] Computer use through xdotool
- [ ] AppImage and deb packaging

## License

Apache-2.0. See `LICENSE`, `Runtime/NOTICE`, and `Runtime/THIRD_PARTY_NOTICES`.
