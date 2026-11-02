# Changelog

## 0.2.0

First public release.

### Changed

- Removed the unused work board, judgment backend, and multi-host floor architecture.
- The floor reads timers, background tasks, open agenda items, and its own failed readings.
- A floor rule can stand autonomy down when its declared condition matches. Only the settings form can start autonomy.
- Pasted screenshots and files copied in Finder attach to the turn.
- Removed upstream project naming from notices, provenance files, and headers.

### Fixed

- A second paste in the same millisecond no longer overwrites the first.
- A stand-down no longer blocks a later run from standing down again.
