# Developer guide

This guide covers building Goblin, locating its implementation boundaries, and
verifying a change. Read [How Goblin works](concepts.md) for the runtime model.
The [User manual](user-guide.md) covers the app's controls.

## Build from a checkout

The build requires Apple Silicon, macOS 26 or later, Xcode with Swift 6.2 or
later, and Python 3.11 or later to run this script. There is no notarized
release. The script downloads the CPython archive pinned in `product.json`.

From the repository root, run:

```sh
python3 scripts/build.py
```

The build downloads the exact Python archive pinned in `product.json`, verifies
its SHA-256, and uses the project-owned build cache. It packages the native app,
Python engine, helpers, help documents, skill assets, and component notices.
The output is `dist/Goblin.app`.

The default signing identity is ad hoc. A source build is not a notarized binary
release. Product identity, versions, provider defaults, native helpers, and
signing identity belong to `product.json`.

To replace an installed app after it has quit, run:

```sh
python3 scripts/build.py --install
```

The installer refuses to replace a running installed instance. Preserve the
active session's work before choosing an update boundary.

## Run checks

Run both language suites with:

```sh
python3 scripts/check.py
```

The script selects the pinned build interpreter when present and clears the
app credential-helper environment before tests. It runs Python unittest
discovery and `swift test`.

The repository workflow in `.github/workflows/checks.yml` uses GitHub’s
`macos-26` Apple Silicon runner. Local development needs the same macOS and Xcode
requirements as a local build.

After building the app, include the packaged-engine fixture with:

```sh
python3 scripts/check.py --packaged
```

That fixture starts the packaged engine in a temporary app home and checks its
native pipe startup and reopen behavior. Without the explicit packaged input,
the fixture skips. Device permissions, real account authentication, microphone
use, and audible output need their own native acceptance checks.

For focused checks, run the test module or Swift test class that owns the
changed behavior. Keep tests isolated from installed credentials and session
data. When importing packaged Python modules directly, use Python's `-B` option
to avoid adding bytecode files to the signed app bundle.

## Locate the implementation owner

| Area | Owning source |
|---|---|
| Product and distribution identity | `product.json` |
| Native chat, conversation presentation | `Sources/ChatViews.swift`, `Sources/GoblinStore.swift` |
| Settings drafts and controls | `Sources/SettingsView.swift`, native settings input helpers |
| Engine connection and generation ownership | `Sources/EngineConnection.swift` |
| Profile catalog, retained engines, and window recall | `Sources/ProfileCatalog.swift`, `Sources/ProfileCoordinator.swift`, `Sources/ProfilePickerView.swift`, `Sources/GoblinApp.swift` |
| Companion preferences and motion | `Sources/CompanionPreferences.swift`, character and rendering sources |
| Native pipe dispatch | `Runtime/goblin_bridge.py` |
| Host lifecycle, intake, and runtime settings | `Runtime/goblin_core/host.py` |
| Retained history catalog | `Runtime/goblin_core/history_store.py` |
| Settings validation and model bindings | `Runtime/goblin_core/config.py` |
| Active turn and tool loop | `Runtime/goblin_core/session.py` |
| Transcript, summary tree, and history view | `log.py`, `tree.py`, `view.py` in `Runtime/goblin_core` |
| Background summary scheduling | `Runtime/goblin_core/compactor.py` |
| Tool discovery, permissions, and execution | `Runtime/goblin_core/tools.py` |
| Owned source labels and model-input envelopes | `Runtime/goblin_core/provenance.py` |
| Skill shelf and governing baseline | `skills.py`, `baseline.py`, and `baseline_skills` in `Runtime/goblin_core` |
| Autonomy and floor evaluation | `autonomy.py`, `autonomy_runtime.py`, `floor_definition.py`, `floor_signals.py` |
| Shared autonomy definitions and profile-owned setup packages | `Runtime/goblin_core/autonomy_setups.py` |
| AetherMind transport and direct adapter | `aethermind.py`, `aethermind_direct.py` in `Runtime/goblin_core` |
| Local core inspection and portable archives | `Runtime/goblin_core/memory_cores.py` |
| Memory inheritance and capture outbox | `Runtime/goblin_core/capture.py` |
| Default core foundation bootstrap | `Runtime/goblin_core/bootstrap_seeds.py`, `Resources/Seeds` |
| Vendored memory primitives | `Runtime/goblin_core/vendor_aethermind` |
| Build, package verification, and release export | `scripts/build.py`, `scripts/release_build.py` |
| Bundled help sources | `Resources/Help` |

The bundled [Native pipe reference](bridge-protocol.md) describes methods,
history paging, snapshots, settings, and core operations. The checkout's
`Runtime/BRIDGE.md` points to that same canonical reference. The repository's
`docs` directory provides focused architecture and verification notes. Source
paths refer to a checkout, not writable locations inside an installed app
bundle.

## Preserve the session contract

Keep one active turn's message prefix fixed. Append tool results and accepted
steering to that chain. Do not replace context or wait for history compaction
inside the active tool loop.

Background summary transports own their cancellation and streaming state.
Publish coarser history views between turns. Admit the next turn only when its
required bounded prefix is complete. Preserve exact recorded source through
indexed reads and zoom.

The governing pstack core belongs outside ordinary conversation history.
Capture it at admission for root and worker turns. Keep optional retrieval
restrictions effective. A summarizer receives an explicit source-only role,
without executing development procedures or repeating the standing baseline.

Worker context and reports have explicit owners. A report delivered
automatically and a report claimed through a wait share the same once-only
delivery boundary. Cancellation must preserve accepted input and actual partial
output.

## Keep history, memory, and autonomy ownership separate

History rotation creates a new session owner with its own log, tree, view, and
history identity. It preserves personality, settings, the selected memory
target, and autonomy setup. The original generation remains selectable. A
retired compactor finishes its existing summaries against that generation;
selection does not cancel its work. Recall reports an
explicit still-finishing condition rather than reusing an active writer.

History mutations enter `GoblinHost.idle_maintenance`. That boundary checks
accepted input, turns, workers, voice, subprocesses, and approvals without
cancelling them. It fences competing session operations and pauses wake
delivery during replacement. Native history pages and notifications carry a
history identity, and chat can supply `expected_history_id` to reject stale
input before acceptance.

`AutonomySetups` stores reusable definitions under `application_home` and live
instances under the profile home. Shared templates contain every autonomy
behavior field, effective World View, full floor, and schedule definitions.
They omit state/task/learning/floor storage bindings and live execution data.
First use derives those bindings inside the profile's own setup directory and
creates independent timers with their recorded due times. Returning to that
template selects the existing profile instance instead of copying the template
over it. Updates to a live instance do not propagate to the shared definition.

Before activation, the controller prepares incoming files and validates them
through native config, floor, state, and learning readers. It retains outgoing
state, complete floor, learning, referenced completed-task outputs, and floor
audit. The original files stay in place. Jobs and live approvals finish before
switching. On process restart, an approved record without a result reports an
interrupted result rather than executing an unavailable closure.

Storage binding changes reconstruct the Autonomy and LearningStore owners.
Register session tool closures after replacement so they capture the incoming
owner. Expert edits to those bindings use the same idle boundary. Full floor
activation uses the internal `save_settings(..., floor_definition=...)` path;
it must not copy outgoing rules over an incoming setup. Reset uses shipped
autonomy defaults, retains the outgoing package, and preserves operator Stop,
personality, model settings, history, and the selected core.

Core creation prepares a separate seeded or empty native store. It stages a
connection rather than activating itself. Rotation uses the normal saved-core
idle boundary and leaves old cores, histories, autonomy packages, and the skill
shelf intact. `memory/create` deliberately bootstraps memory with
`stage_skills=False`; creating or inspecting a core does not rewrite a user's
skill procedures.

## Preserve profile and window ownership

`ProfileCatalog` stores stable IDs in `<application_home>/profiles.json`.
The default profile keeps `application_home` as its home and standard native
preferences. Named profiles use `<application_home>/profiles/<uuid>` and the
`<bundleID>.profile.<uuid>` UserDefaults suite. The display name comes from the
profile's `config.json` `agent_name`, including after restart. Creation starts
the private store, saves its name through ordinary settings validation, and
then publishes its stable catalog identity.

`ProfileCoordinator` retains one `GoblinStore` and private engine per loaded
profile. Recall here rebinds presentation while the outgoing store and work
remain retained. Open in new window adds a presentation for another profile;
recalling an already presented profile focuses its existing window. Keep
profile-specific views and preferences bound to their owning store. App launch
presents the default profile, and app termination shuts down retained engines.

`GoblinStore` passes profile home and shared application home separately to
`EngineConnection`. The bridge accepts `--application-home`, defaulting to
`--home` for standalone callers. Config, history, default core, cache, registry,
and live autonomy data remain profile-scoped. The default custom skill shelf
also belongs to the profile; an explicitly configured shelf can choose another
location. Bundled pstack assets are common product resources. Autonomy template
definitions use the shared application home, while their instances stay local
to the profile. Existing AetherMind local or remote connection settings provide
explicit core sharing.

Profile identity is native window/store context. The Python pipe does not add
profile-management RPCs or a profile ID field to snapshots. Every pipe already
belongs to one profile home. Product Keychain service identities remain common
to the application; model route settings belong to each profile.

## Change settings at their owning boundary

Native settings use a local draft and an expected settings revision. Streaming
telemetry has a different revision. A save validates the complete configuration
before replacement and reports a later runtime-apply failure separately.

Preserve independent custom and account model choices. Saved custom model
entries contain reusable route settings, with credentials kept in their
endpoint-scoped Keychain items. Compression has its own role fields or inherits
chat. A saved dormant compression value does not identify the active route.

`custom_models` is a top-level object of reusable named route records. Its keys
identify presets. Each record contains `name`, `model_base_url`, `model`,
`api_kind`, `cli_command`, `reasoning`, and `thinking_budget`. Applying a preset
copies the fields supported by the selected role into that role's canonical
settings. It does not introduce another live model binding. Custom compression
supports HTTP protocols, while CLI worker presets apply to chat.

Personality and world view remain freely editable prose. Assisted creation
provides a format only during that flow. Floor definitions retain the engine's
existing functional schema. Keep settings and the saved definition's effective
world view aligned so a successful save changes the actual wake context.

Tool controls must use the same registry and disabled settings as execution.
Expose disabled tools so the user can re-enable them. Add and remove external
providers through MCP configuration rather than inventing parallel tool owners.

Tool snapshot metadata reports `risky`, matched `approval_classes`, and the
existing `approval_rule`. **Pause acting tools** adds registered risky or
acting-class tool names to the draft disabled list. **Enable all registered
tools** clears registered names while preserving disabled entries for unloaded
tools. Both use normal revision-checked Save settings. Neither changes approval
mode, automatic capture, process isolation, or tool execution semantics.

Preserve Goblin's advanced-user power-tool model. Ask when needed uses its existing approval
rules; Unleashed runs enabled tools without approval prompts and retains the
process's access. Keep those controls and connection exposure visible. Users
can configure read-focused tools or external containment. Do not turn those
optional choices into a mandatory sandbox, new runtime caps, or additional
execution gates.

Preserve provenance when presenting retrieved content or memory to the model.
Distinguish a source's quoted instruction from actual user authority. Preserve
memory claims as sourced claims until interaction or other evidence supports
them, rather than promoting them into verified user preferences. These
distinctions retain the user's intent without adding a separate policy engine.

`provenance.py` supplies owned origins and source envelopes for model requests.
Worker reports retain their parent source. New Gist nodes carry bounded origin
lineage and covered record ranges. Older parent nodes without saved lineage use
`historical_mixed` attribution. Raw saved transcript text is unchanged. Native captures and notes
use `goblin-origin:*` markers with source-record evidence. Ordinary `source:*`
user tags remain unchanged, and a user-supplied owned-prefix tag is recorded as
a declared literal. Imported markers are not authenticated authority. These
labels support attribution, not proof of truth or model behavior.

## Preserve memory meaning and ownership

Use the native AetherMind implementation and adapter contracts. Preserve full
bodies, currentness, correction relationships, unresolved debt, and separate
authored texture. An inheritance packet is scoped context delivery, not a full
core archive or proof of model use.

The embedded core is the default. Remote MCP project paths belong to the host
that owns the store. Keep the active turn's client, inheritance, and capture
target stable when preparing a different connection.

The host bootstraps generic foundations into its default app-owned embedded
core before constructing capture or preparing the first inheritance. The
installed content comes from `Resources/Seeds/pack.json`; `SOURCE.json` records
its origins and adaptations. Repository proposals are not bootstrap inputs.

`bootstrap_seeds.ensure_bootstrap` uses the canonical memory library to append
missing bodies. Content hashes make repeated runs idempotent. Existing native
records and user skill directories remain intact. Explicit local attachments,
imports, and remote cores are excluded from automatic reseeding. An invalid
store stays in place and the result is exposed through `memory.foundation`
and the native Continuity warning. Bootstrap knowledge supplies general product
and quality guidance rather than private history, personality, or authority
over the user's instructions.

A local export preserves native store files and hashes. Import inspection and
supported adaptations operate on a separate copy. Preserve the source archive
and store. Treat unknown authored fields as retained data rather than dropping
them through a reconstructed layer list. Report unsupported formats and derived
tree requirements accurately.

Pending capture writes belong to their original target and request identities.
Switching cores must not deliver old debt to a new store merely because its
project string matches.

The advanced connection object is:

```json
{
  "aethermind": {
    "mode": "embedded",
    "local_project": "",
    "project": "",
    "endpoint": "",
    "token_env": ""
  }
}
```

This is a fragment of the `advanced` object. `embedded` selects a local core.
An empty `local_project` uses Goblin's application-support store. `remote`
selects the existing HTTP MCP client, with `endpoint` and the owning server's
`project` path or key. `token_env` optionally names an environment variable,
never the credential value.

The native pipe exposes these operations:

- `memory/inspect {connection}` inspects the draft local or remote connection
  without activating it.
- `memory/export {destination}` exports the active local store.
- `memory/import {archive}` prepares an isolated local copy and returns its
  `project_root`, `inspection`, and `repairs` report.

An import does not activate itself. A settings save selects the imported
`local_project`, and a running turn retains its previous client until the idle
boundary. An explicit local attachment must already be a readable, compatible
core. Blank local selection retains default embedded initialization. Remote
inspection uses existing MCP capabilities and status methods.
Local archive operations do not invent a remote export endpoint.

The bundled implementation identifies its engine as `0.3.0`, native format as
`light-v1`, and adapter contract as `adapter-v1`. Inspection distinguishes those
version domains. It preserves missing or corrupt derived-tree data for review
and reports any rebuild requirement. Import does not call a model to invent
summaries or guess an unknown-format conversion.

## Verify the user journey

Reproduce the affected behavior at its owning boundary. Use focused tests to
protect the change, then exercise the native path that the user follows.

For a settings change, edit, save, reload, and verify the runtime's effective
value. For history work, inspect actual saved records and request prefixes.
For core transfer, compare the archive and imported native file hashes, inspect
the result, and prove that the original remains unchanged.

Build a replacement bundle and check its source identity and signature. Test
startup with a fresh temporary home. Preserve a running installed session while
the replacement is staged. Passing unit tests alone does not establish the
installed behavior or live account and hardware acceptance.

## Prepare a local release candidate

Start with reviewed, committed source and a clean tracked tree, then run:

```sh
python3 scripts/build.py --release
```

The release build exports tracked `HEAD`, builds from that export, and writes
a source archive and release identity in `dist`. The identity records the
source tree, archive checksum, and executable checksum. Source and debug paths
are mapped to relative paths during compilation.

This prepares a local candidate. It does not publish a repository, upload an
artifact, or notarize the app. Keep app data, credentials, private deployment
values, and development history out of a public export.

## Review licenses and artwork

Application and adapted-runtime source use Apache-2.0. The embedded AetherMind
component retains its AetherMind Noncommercial Public License. pstack skill
assets retain their MIT license and attribution. The bundled interpreter and
native dependencies retain their own notices.

Read `LICENSE`, `Runtime/LICENSE`, `Runtime/NOTICE`,
`Runtime/AETHERMIND_LICENSE`, `Runtime/THIRD_PARTY_NOTICES`, and the license texts
under `Runtime/THIRD_PARTY_LICENSES` before redistribution. The bundled character artwork is distributed with this project. Component
notices identify dependencies with separate license terms.

## Use Goblin as a development assistant

Select the source checkout as Goblin's workspace. Ask it to read this guide,
identify the owning code, and inspect current source before proposing a change.
Its documentation tools provide the bundled user and developer guides, while
ordinary file tools inspect the actual checkout.

The `app_help` tool exposes the shipped catalog through these actions:

```json
{"action": "index"}
{"action": "search", "query": "stable prefix"}
{"action": "read", "document": "developer-guide", "start_line": 1, "line_count": 100}
```

Read results report `start_line`, `total_lines`, page text, and `next_line` when
more content remains. Search matches include line numbers. Read pages are
limited to 400 lines. The catalog is
`Resources/Help/index.json`, and its entries refer to files in that directory.
The native viewer and runtime tool use the same document sources. Bundled help
reflects the installed build and does not update itself from the network.

Review the diff and run checks against the changed behavior. Build the app from
the checkout and choose a restart boundary that preserves current work. Keep
the source revision, built artifact, and installed process distinct when
reporting what has been verified.
