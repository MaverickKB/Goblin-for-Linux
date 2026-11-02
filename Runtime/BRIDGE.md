# Native pipe reference

The canonical [native pipe reference](../Resources/Help/bridge-protocol.md) ships
with the app and is available through its Help viewer and `app_help` tool.

`goblin_bridge.py` owns dispatch. `goblin_core/host.py` owns runtime operations,
and `goblin_core/config.py` owns settings validation. The private-pipe protocol
connects the native app to its bundled engine.
