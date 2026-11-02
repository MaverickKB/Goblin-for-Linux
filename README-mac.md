# Goblin for Mac

Goblin is a native Mac desktop agent with a character, a continuing conversation,
and tools that can do real work. Give it a project, ask it to use your Mac,
write or modify files, investigate a problem, or leave it with a standing task.
Its personality and priorities are yours to define.

Use a local model server, save several custom model connections, or connect a
supported account. Goblin runs the agent harness on your Mac. It connects to
the model you choose; it does not install or serve that model.

## Get started

1. Build and open the app using the instructions below.
2. Open **Settings > Connections**, enter your server's API base URL and model
   identifier, choose its protocol, and save. Saved model entries make it easy
   to switch between servers and models. Account connections are available in
   the same tab.
3. In **Goblin** settings, choose a name, avatar, workspace, and personality.
4. Send a request in chat.

**Ask when needed** asks before enabled tools that require approval.
**Unleashed** runs those tools without asking. The Tools tab lets you control
which tools are available. Neither mode is an operating-system sandbox. Use
permissions and an environment appropriate to the access you give the agent.

## Give it ongoing work

Under **Settings > Autonomy**, describe the standing task in **World view**,
choose a **Floor interval**, save, and turn Autonomy on. The floor can start a
turn in the same conversation when the interval elapses and the session is
available. For example:

> Watch new pull requests in my project. Review changes, fix small bugs, and
> merge only when CI passes for the latest commit. Keep tests and CI intact;
> ask me about changes outside that scope.

World view gives the agent purpose. Its tools supply observations and perform
work during the resulting turn. Mentioning a machine or service does not
connect it or grant access. Configure those connections separately.

A new Goblin has an empty World view and no assigned monitoring job. Its
shipped floor has no rules or scripted assignments; you supply its purpose.
It does not sample your Mac's CPU, memory, disk, or processes, manage machines,
or ship anyone's personal watch configuration. Add the work you actually want.
Session timers and background tasks are additional tools; a recurring floor
session does not require the model to create a timer.

Pause Autonomy to stop further self-initiated delivery. **Stop** cancels current
work and fences further autonomous wakes until you continue with a new message.
A floor rule with `action: stand_down` lets Goblin switch autonomy off when a
condition you defined matches, such as a finished task or a deadline. It stays
off until you turn it on here; nothing else can start it.
Advanced autonomy templates save World view, the full floor, configuration, and
schedule definitions. Each Goblin keeps its own execution state when using a
shared template.

## Multiple Goblins

Use the name dropdown at the upper left of chat to recall a profile, open it in
a new window, or create a new Goblin. Each profile has its own avatar,
personality, workspace, model settings, conversation history, default memory
core, and live autonomy state. Switching away keeps its retained engine alive.
An already open profile uses its existing window.

Goblins can share saved autonomy templates. Sharing a template does not share
running jobs or queued wakes. They can also share an AetherMind core when you
explicitly connect each profile to that core.

## Continuity and working skills

**Gist** keeps model history bounded with summaries of older transcript ranges.
Zoom retrieves their saved source. Summaries are built in the background;
active turns retain their existing context prefix. A new turn can wait for
pending history compression before its first model call.

**AetherMind** stores learned experience separately from conversation history.
The default embedded core begins with generic product knowledge and working
lessons, then learns from your interactions. Continuity settings can attach an
existing local or remote core, export a local backup, or import an inspected
copy while preserving the original.

The **pstack operating core** supplies baseline working procedures to every
root and worker turn. Goblin selects specialized skills as the work requires.
You do not need to invoke its baseline manually.

Advanced controls rotate history, memory cores, or autonomy setups separately.
The outgoing data is retained for recall.

## Build from source

Requirements: Apple Silicon, macOS 26 or later, Xcode with Swift 6.2 or later,
and Python 3.11 or later to run the build script.

```sh
git clone https://github.com/MaverickKB/Goblin-agent-for-Mac.git
cd Goblin-agent-for-Mac
python3 scripts/build.py
```

The build produces `dist/Goblin.app` with its own pinned Python runtime.
`product.json` owns the app identity, dependency pin, and build defaults.
The current signing identity is ad hoc; the app is not notarized.
After quitting an installed copy, `python3 scripts/build.py --install`
replaces it. Build and packaging details are in the Developer guide.

```sh
python3 scripts/check.py
```

After building, `python3 scripts/check.py --packaged` also checks the packaged
engine. Voice, microphone permissions, and real computer-use permissions need
native device verification.

## Help

The app's **Help > Goblin Help** viewer and Goblin's `app_help` tool read the
same manuals:

- [User manual](Resources/Help/user-guide.md): setup, profiles, tools, autonomy,
  memory, voice, and window controls.
- [How Goblin works](Resources/Help/concepts.md): Gist, AetherMind, stable turns,
  and autonomy ownership.
- [Developer guide](Resources/Help/developer-guide.md): architecture, builds,
  tests, and modifying Goblin's source.
- [Native pipe reference](Resources/Help/bridge-protocol.md): the app's private
  engine protocol.
- [Repository documentation](docs/README.md): focused runtime references.

Voice uses an xAI connection or voice API key. The character and empty chat
header support dragging; motion, always-on-top, hold-to-talk, and custom avatars
are configurable.

## Source and licenses

SwiftUI and AppKit own the interface. Each loaded profile owns a bundled Python
engine communicating through private stdin/stdout pipes. The app opens no
local listening server. User data and credentials belong in installed app
storage, outside this repository.

Application source uses [Apache-2.0](LICENSE). The embedded AetherMind component retains its
[AetherMind Noncommercial Public License](Runtime/AETHERMIND_LICENSE), which
does not grant commercial use. pstack skill assets retain their MIT notice.
Read [Runtime notices](Runtime/NOTICE) and
[third-party notices](Runtime/THIRD_PARTY_NOTICES) before redistribution.
The bundled character artwork is distributed with this project.
