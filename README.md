# Goblin for Linux

A native Linux agent companion built with Tauri 2 and the Goblin Python engine. Same product contract as the Mac app: one continuing conversation per Goblin profile, local tools, embedded AetherMind continuity, and an autonomy floor whose on switch belongs to the operator.

This port is in progress. The engine is vendored from Goblin for Mac 0.2.0. The Linux front end, audio, and packaging are being built here.

## Architecture

- `Runtime/` is the Goblin Python engine. It speaks NDJSON over stdin and stdout, one frame per line, exactly as it does on the Mac.
- `Resources/` holds the manuals, seed pack, and character art.
- `src-tauri/` is the Tauri shell. It spawns the engine, owns the window, audio, notifications, and tray.
- `src/` is the web UI.
- `scripts/goblin-credentials` is the Linux credential helper. It speaks the same JSON protocol as the Mac helper and stores secrets in the desktop keyring through `secret-tool`.

## Install

Download `Goblin_0.1.0_amd64.deb` from Releases and install it:

```sh
sudo apt install ./Goblin_0.1.0_amd64.deb
```

Run Goblin from the applications menu or with `goblin`. On first run, open Settings, set a model base URL and model, or sign in with xAI or OpenAI under Connections. Credentials live in the desktop keyring. Profile data lives in `~/.local/share/goblin`.

## Known limitations in 0.1.0

- Voice is not ported yet.
- No tray icon yet.
- The Debian package is the only published format; AppImage is not built.
- The character uses Canvas 2D; the Mac mesh deformation and face shader are pending.
- Live xAI device sign-in needs a desktop keyring and is not exercised in CI.

## Development

Requirements: Ubuntu 24.04 or later, Rust 1.77 or later, Node 20 or later, and the Tauri Linux dependencies (`libwebkit2gtk-4.1-dev`, `libgtk-3-dev`, `librsvg2-dev`, `libayatana-appindicator3-dev`, `libasound2-dev`, `libsecret-1-dev`, `libsecret-tools`). Python 3.11 or later is needed for the test suite and build scripts; packaged builds bundle CPython 3.12.16.

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

- [x] Engine vendored and running on Linux Python, 405 tests green
- [x] Linux credential helper
- [x] Tauri shell, chat, history, composer
- [x] Settings: personality, model connection, autonomy switch
- [x] Autonomy floor editor and continuity summary
- [x] Tool enablement controls
- [x] Tool approvals
- [x] Attachments: picker, drag and drop, transcript chips
- [x] Connections panel and device sign-in flow
- [x] Debian package
- [x] Bundled CPython runtime (3.12.16)
- [x] Character: standing art with breathing, sway, glances, and blinks (Canvas 2D)
- [ ] Character mesh deformation and face shader (WebGL)
- [ ] Voice
- [ ] Notifications and tray
- [ ] Computer use controls
- [ ] AppImage
- [x] Bundled Python runtime

## Build notes

Build the package with `bash scripts/build-deb.sh`. It fetches the pinned CPython 3.12.16 runtime with a checksum (`scripts/fetch-python.sh`), clears the app crate, and builds with `CARGO_INCREMENTAL=0`. Cargo can reuse a stale frontend asset bundle after UI changes; the script clears the crate first. For a quick release binary without a package, run `bash scripts/fetch-python.sh`, then `cargo clean -p goblin` inside `src-tauri`, then `npm run tauri build -- --no-bundle`.

The packaged app runs its engine and credential helper with the bundled interpreter at `<resources>/_up_/vendor/python/bin/python3`. CPython is distributed under the PSF License; the runtime is fetched at build time and is not committed.

## License

Apache-2.0. See `LICENSE`, `Runtime/NOTICE`, and `Runtime/THIRD_PARTY_NOTICES`.
