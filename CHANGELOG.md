# Changelog

## 0.1.2

Parity pass on the remaining surfaces.

### Changed

- The character now deforms with the Mac rig mesh: head, arms, jaw, chest, and body weights drive a 12 by 24 triangle grid on Canvas 2D. WebGL hangs in the target WebKit build, so the mesh is rendered with clipped 2D triangles.
- Computer use has controls, not only inspection: click the screenshot to click that point, type text, press key combos, focus a window, and scroll. Each input tool goes through the engine approval path.
- The approval card lives above chat and settings, so it stays visible while configuring.
- Tray icon with Show Goblin and Quit.
- AppImage is built from the same AppDir and runs on the bundled CPython.

### Added

- Voice session path: engine lease, websocket with the issued subprotocol, session update, microphone PCM16 frames, playback, and engine event forwarding. It requires an xAI sign-in and a microphone; the headless build host can only verify the sign-in gate.

## 0.1.1

The window now matches the Mac companion.

### Changed

- Borderless transparent window, 898 by 674, with the goblin standing free on the desktop.
- The chat is a 620 by 650 light card with a 22 px radius and 2 px border, beside the character.
- Collapse hides the card, shrinks the window to the character, and shows the latest reply in a speech bubble above the goblin. Clicking the bubble opens the chat again.
- Settings opens inside the card with a Back to chat control.
- Header shows the agent name, model, turn count, autonomy pill, settings, and collapse.
- Composer matches the Mac structure: tall message box, attach, Voice, Stop, Send, and the Return hint.

## 0.1.0

First public Linux build.

- Chat, history, attachments, approvals, tool enablement, autonomy floor, continuity, connections, computer inspection, and notifications.
- Canvas 2D character with breathing, sway, glances, and blinks.
- Debian package with bundled CPython 3.12.16 and no system python3 dependency.
