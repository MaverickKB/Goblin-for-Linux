# Linux port plan

The Mac app is one SwiftUI process over a Python engine. The Linux app is one Tauri window (Rust) over the same engine. Everything below the window is shared; everything above it is rebuilt.

## Shared and already working

- `Runtime/` engine: chat, tools, history, compaction, autonomy, AetherMind, accounts, voice, computer use.
- `Resources/`: manuals, seed pack, character art.
- The NDJSON pipe contract in `Resources/Help/bridge-protocol.md`.

Linux changes in the engine so far:

- Startup summary-tree replay writes once per level instead of one fsync per message. A 3,000-message history opened in 19.9 seconds before the change.
- `Tests/Python` runs on Python 3.12. Two macOS packaging tests were removed; Linux packaging tests come with the release pipeline.

## Host surfaces to rebuild

| Surface | Mac | Linux |
| --- | --- | --- |
| Window and UI | SwiftUI | Tauri 2, HTML/CSS/JS |
| Credentials | Signed helper, Security framework | `scripts/goblin-credentials` over `secret-tool` |
| Microphone and playback | AVAudioEngine | Rust `cpal`, 24 kHz mono PCM16 |
| Voice websocket | Swift | Rust `tokio-tungstenite` |
| Computer use | Accessibility helpers | engine `LinuxCua` over xdotool, wmctrl, scrot |
| Notifications | UserNotifications | D-Bus through `notify-send` or a Tauri plugin |
| Menu bar | NSStatusItem | tray icon |
| Packaging | `.app`, ad-hoc codesign | `.deb` and AppImage, bundled Python runtime |

## Phases

1. Engine on Linux, tests green. **Done.**
2. Window, chat, history, composer, model settings, autonomy switch. **Done.**
3. Attachments: file picker, drag and drop, transcript chips. **Done.**
4. Settings parity: connections panel done; autonomy floor, continuity, and tools remain.
5. Character: avatars, eyelids, standing art, motion, reactions. Not started.
6. Voice: capture, playback, websocket, transcripts, tool calls. Not started.
7. Accounts: device sign-in, model catalog, credential use. Panel and flow built; live provider sign-in not yet exercised.
8. Notifications, tray, close-to-tray. Not started.
9. Computer use: window list, screenshots, input, approvals. Engine tools exist; dedicated controls remain.
10. Packaging, bundled runtime, desktop entry, release checks. Debian package installed and chatted; AppImage and bundled Python remain.

Each phase is done only when it runs on the target and has a direct probe.

## Decisions

- Keep the engine directory named `Runtime/` so its paths, tests, and manifests stay identical to the Mac app.
- Keep the engine's own credential protocol and implement it in a Linux helper. No engine-side credential branches.
- Bundle a pinned CPython with the app. The system interpreter is for development only.
- Target Ubuntu 24.04 first, then Debian 12 and Fedora 40.
