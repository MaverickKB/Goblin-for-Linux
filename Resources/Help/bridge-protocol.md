# Native pipe contract

Each line is UTF-8 JSON. A request contains `id`, `method` and an object `params`.
A response contains the same `id` and either `result` or `error.message`.
Notifications contain `event` and `data`. stdout carries protocol frames;
stderr carries redacted diagnostics. The app owns the engine through private
stdin/stdout pipes. It opens no listener.

For example, a native caller can request a snapshot with:

```json
{"id":"check-1","method":"snapshot","params":{}}
```

Method signatures below describe parameter types. `?` marks an optional field.
Actual frames contain ordinary JSON values.

Launch the bundled entry point with `--home <profile_home>` and
`--manifest <product.json>`. Optional `--application-home <shared_home>` selects
the shared autonomy-template root. It defaults to `--home`. Each engine process
owns one profile home and one session; named-profile window selection belongs
to the native app.

## Dispatch and intake

The reader handles Stop and approval resolution independently of slow work.
Snapshots, history reads, settings, Autonomy, tools, account, memory, media, and
floor operations run off the reader. Chat send, queue, and steering frames enter
one FIFO intake lane. Each chat frame carries the cancellation generation
captured when it arrived.

A successful send or queue response acknowledges durable acceptance. Pending
input is fsynced at `<home>/intake/pending.json` before acknowledgement. It is
removed only after its authoritative user row exists in the raw ChatLog.
Queued input does not enter model context until execution. Setup loss, Stop,
failure and shutdown retain accepted text, request identity and attachment
references. On restart, unfinished intake becomes canceled input, never replayed
work. Repeated identities cannot replace accepted content. Malformed journals
are preserved as `pending.json.corrupt-*`; valid entries remain recoverable and
the snapshot reports the preservation location.

Steering returns true only after one raw user row is fsynced with its owning
turn identity. The same row is injected at the next model boundary without a
second append. An interruption or failure cannot discard accepted direction.
Late steering returns false. Shutdown fences intake before acknowledgement and
then closes owned work; it does not change the operator's saved Stop preference.

## Methods

- `bootstrap {}` and `snapshot {}` return the live snapshot.
- `history/page {before?:integer, limit?:integer}` returns presentation rows
  under `messages`, the next `before` cursor, `has_more`, and the raw-record
  `total`. Omitted `before` starts at the current end. The default limit is 80
  raw records and accepted limits are 1 through 200. Each page has a bounded
  presentation payload. A raw record can produce several presentation rows.
  The result includes the selected `history_id`.
- `history/record {i:integer, offset?:integer}` reads exact recorded JSONL bytes.
  It returns base64 `data`, byte `total`, and `next_offset`. The default offset
  is zero. Chunks contain at most 16,384 source bytes. Continue until the next
  offset equals the total, then decode the combined record.
  The result includes `history_id`.
- `history/gist {token?:string, offset?:integer}` reads an immutable Gist byte
  snapshot. Start with no token at offset zero. The result contains base64
  `data`, byte `total`, `token`, and `next_offset`, with chunks up to 16,384
  bytes. Supply that token for subsequent pages. Completion releases the
  snapshot. A new read replaces the previous token, and an expired token
  reports an error.
- `history/list {}` returns `{history_id, histories:[{id,label,created,active}]}`.
- `history/rotate {}` creates and selects a retained history generation and
  returns its snapshot. Identity, settings, core, and autonomy setup remain.
- `history/select {id:string}` selects an existing generation and returns its
  snapshot. Running turns, workers, voice, jobs, approvals, and accepted input
  must settle before either history mutation. Background summaries finish
  against their old history; recall can report that it is still being released.
- `chat/send {text, attachments?:[absolute_path], request_id?:string,
  expected_history_id?:string}` returns
  `{turn_id}`. `chat/queue` accepts the same fields and adds `queued:true`.
  A mismatched history identity is rejected before durable acceptance.
- `chat/steer {text}` returns `{accepted:boolean}` and may add `fenced:true`.
- `chat/stop {}` returns `stopped:true`, the active turn identity and canceled
  queued identities. It cancels work and preserves accepted input.
- `settings/save {settings, expected_revision?:integer}` returns the committed
  snapshot. Revision conflicts and pre-commit validation errors leave settings
  unchanged. A post-commit runtime apply failure returns the new revision with
  `settings_issue` and `ready:false`. A successful later save clears the issue.
- `autonomy/set {enabled:boolean, expected_revision?:integer}` uses the same
  settings and lifecycle owner and returns its exact mutation snapshot.
- `autonomy/templates {}` returns `templates` and profile-private `setups`
  arrays of `{id,name}`, plus `active_id`. A shared template with a retained
  local instance appears once under its shared identity.
- `autonomy/template/save {name:string, expected_revision?:integer}` copies
  saved behavior, effective World View, full floor, and schedule definitions
  into the shared catalog. It returns the catalog, `saved_id`, and `snapshot`.
  It does not activate the template or copy live execution state.
- `autonomy/template/select {id:string, expected_revision?:integer}` selects
  the profile's existing instance or prepares an independent first-use instance.
  `autonomy/reset {expected_revision?:integer}` prepares shipped autonomy
  defaults. Both retain the outgoing setup and return the catalog plus
  `snapshot`. Jobs and approvals must finish first. Operator Stop is preserved.
- `approval/resolve {id, accepted:boolean}` returns `{id, accepted}`. Approval
  notifications expose the actual tool name and complete arguments.
- `floor/read {}` and `floor/save {definition:string, expected_revision?:integer}` return definition and
  status. Reads export JSON. A save accepts JSON or TOML text, validates it, and
  writes the floor file as JSON.
- `tool/execute {name, arguments}` uses the session ToolRegistry, approvals and
  actual result/diff receipts.
- `memory/read {}` returns the active AetherMind target's status, inheritance,
  and recent layers.
- `memory/inspect {connection?:object}` inspects a draft AetherMind connection
  without activating it. The object uses the `advanced.aethermind` fields.
  An omitted connection inspects the active target. Local inspection reports
  native compatibility and integrity. Remote inspection uses existing MCP
  capabilities and status methods.
- `memory/export {destination:string}` exports the active local core to a new
  archive path and returns its path and file manifest. It preserves native
  bytes. This method does not export remote stores.
- `memory/import {archive:string}` prepares an isolated local copy and returns
  `project_root`, `inspection`, and `repairs`. It leaves the source archive
  and original store unchanged. Import does not activate the copy.
- `memory/create {seeded:boolean}` prepares a separate local core and returns
  `project_root`, `inspection`, `seeded`, and `bootstrap` (null for empty cores).
  It does not select a connection. Save `advanced.aethermind.local_project` to
  activate it. The old core, history, autonomy setup, and skill shelf remain.
- `account/start {provider?:string}` starts the selected account's sign-in.
  `account/status {provider?:string}` returns its current sign-in state.
  Results expose verification URL, user code, and grant health, never token
  values. No sign-in starts automatically.
- `account/list {}` returns `{accounts:[...]}` for configured providers.
- `account/cancel {provider?:string}` cancels sign-in and any active connection
  check owned by that provider.
- `account/check {provider?:string}` checks credentials and retrieves the
  provider's model catalog. Its result contains `provider`, `ok`, `models`, and
  `message`. Account operations default to `xai` when the provider is omitted.
- `credentials/authorize {role:"account", provider:string}` requests access to
  that provider's saved item only after an explicit native user action.
  Roles `model` and `voice` authorize their own items. Model requests require
  `endpoint`. The result contains status only, never a credential value.
- `voice/check {}` checks the selected voice authentication.
- `voice/start {}` returns the app-owned voice lease. `voice/events
  {lease_id, events}` returns accepted events, messages and session results.
  `voice/stop {lease_id}` and `voice/heartbeat {lease_id}` control that lease.
- `shutdown {}` acknowledges closure, drains received intake under the shutdown
  fence, closes owned work and exits. EOF follows the same lifecycle.

## Snapshot and settings

Snapshot fields include `revision`, `settings_revision`, `settings_issue`,
`history_count`, `history_id`, `ready`, `setup_issue`, `busy`, `active_turn_id`, `turns`,
`model`, `backend`, `workspace`, `compaction`, `memory`, `autonomy`, `settings`,
`tools`, `pending_approvals`, and `error`. The snapshot contains bounded turn
state, not transcript messages or the full Gist. History methods read those
separately. Turn rows are limited to 64 and retain the active turn. Settings
revision belongs to the configuration document, not streaming telemetry.
Turn states are `queued`, `running`, `done`, `error`, and `cancelled`.

Tool rows include `name`, `description`, `enabled`, `origin`, `risky`, matched
`approval_classes`, and the existing `approval_rule` (`allow` or `gate`). The
list includes disabled registrations so native controls can re-enable them.
These fields describe active registry settings, while native tool controls edit
a draft until Save settings. Model schemas and execution continue to enforce
the registry's actual permissions. Metadata adds no approval or isolation rule.
Memory state includes its active `mode` and `project`, capture `debt`, layer
count, and `pending_change` when the saved connection differs from the active
turn's target.

`memory.foundation` reports bootstrap state. Status `ready` describes a completed
default-core bootstrap; `unavailable` includes a bounded `error` and
`error_class`; `external` means the selected core is outside default automatic
bootstrap. Default-core reports also include `seed_version`, appended and
skipped layer and texture counts, and staged and retained skill counts. This
metadata reports initialization work. It does not prove model recognition or
application of that knowledge.

The raw ChatLog owns message identity and history. Native pending rows are
provisional and de-duplicate against authoritative user rows. Message rows
include their turn, response and tool-call identities, status, attachment paths
and actual diffs where available. Stream and activity notifications retain the
active turn and response identity, so interim replies remain distinct messages.
Message, stream, activity, and compaction events also carry `history_id`.
Native callers discard events and paged results from a previously selected
generation rather than mixing them into the current history.

Owned provenance accompanies model-input envelopes and new summary-node
lineage. Native captures retain source markers and record evidence. Saved
transcript text and the pipe framing remain unchanged. These source labels aid
attribution; they do not authenticate imported claims or establish permissions.

Settings fields are agent name, instructions, workspace, model connection and
API kind, CLI command, reasoning, thinking budget, approval, world view, floor
interval, voice model/name/authentication, `custom_models`, and `advanced`. The
Config validator owns their exact names and values. Advanced sections are
tools, skills, MCP servers, remotes, Autonomy, computer use, CUA, compactor, and
AetherMind. Unknown fields and secret-bearing keys are refused. Names such as
`token_env` identify a credential source rather than storing its value.
Blank model settings keep the Settings repair
lane usable and leave chat unready. Workspace adoption is validated before the
configuration document is replaced.

The top-level `model`, `model_base_url`, `api_kind`, `cli_command`, `reasoning`
and `thinking_budget` belong to the custom connection. `model_connection`
selects the active chat connection. `account_models` maps manifest provider ids
to independent `{model, reasoning, thinking_budget}` objects. Changing the
selected account never changes the custom fields or another account's options.
An account with no selected model remains unready rather than borrowing the
custom model name.

`custom_models` holds reusable presets keyed by stable identifiers. Each record
contains a nonblank `name` and route fields `model_base_url`, `model`, `api_kind`,
`cli_command`, `reasoning`, and `thinking_budget`. Applying a preset copies the
fields supported by chat or compression into that role's canonical settings.
The preset is not another active binding. CLI worker presets apply to chat;
custom compression protocols remain HTTP-only.

`advanced.compactor` keeps its custom model, endpoint and protocol separately.
Its `account_models` map owns the compression model choices for each provider.
An empty `connection` uses the chat connection. `inherit_model` overrides the
model only when that chat connection is custom; account inheritance uses that
provider's compression choice or the chat model. With legacy custom chat, a
separate compression endpoint becomes an explicit custom connection. Documents migrate in
memory on load and persist the new fields on the next settings save.

`advanced.aethermind` accepts `mode`, `local_project`, `project`, `endpoint`, and
`token_env`. Mode `embedded` selects the in-process adapter. An empty local
project uses the app's default store. An explicit local attachment must already
be a readable compatible core. Mode `remote` uses the existing HTTP MCP client
with the owning server's project path or key. The token field names an optional
environment source and never stores its value.

A saved core change becomes active at an idle boundary. The current turn
retains its captured client, inheritance, and capture target. Pending capture
writes retain their original target-specific outbox rather than being delivered
to the newly selected store. Local import preserves an independent copy and
does not synchronize it with its source.

Personality and world view remain editable prose. Assisted creation supplies a
format without adding runtime contract enforcement. Floor definitions keep the
engine's existing functional validation. The settings snapshot reports the
effective saved floor world view, and saves keep its owning definition aligned.

`advanced.autonomy.floor_file` explicitly binds the active floor definition.
Blank retains the legacy home-scoped path. State, task directory, learning file,
and floor binding changes use the same idle maintenance boundary as setup
selection. The lifecycle reconstructs their owning Autonomy and LearningStore
and then registers tool closures against that owner. Incoming setup assets are
prepared and validated before the settings commit. Runtime apply failures
remain visible through the committed snapshot's `settings_issue`.

## App-owned data and credentials

In the native app, `<application_home>/profiles.json` catalogs stable profile
IDs. The default profile uses `<application_home>` as `<home>`. Named profiles
use `<application_home>/profiles/<uuid>`. Profile management is a native concern,
with one private pipe and engine per loaded profile. The wire snapshot has no
profile ID; the owning native store supplies that context. Native avatar and
companion preferences are profile-scoped. Credential service identities remain
application-wide.

The bundled credential helper owns all OS Keychain operations. Its code and
credential metadata are signed independently of the UI executable. Its version
tracks the credential protocol rather than each application release. Unchanged
helper inputs preserve its signing requirement across UI updates.

Automatic reads, writes, removals and status checks prohibit OS interaction.
Protected items report `keychain_required` in Connections. Only the native
Allow Keychain access action permits a permission dialog. Existing items remain
in place when access is denied. The runtime has no system `security` fallback.
An existing installation may require explicit authorization for the helper to
access items created by the earlier main executable.

- Configuration: `<home>/config.json`.
- Original chat: `<home>/chat/<session_slug>/{log,tree,view.json,attachments,outbox}`.
- History catalog: `<home>/history/<session_slug>.json`; additional histories:
  `<home>/histories/<session_slug>/<history_id>`.
- Alternate memory targets: target-specific outboxes under the session's
  `memory-outboxes` directory.
- Pending intake: `<home>/intake/pending.json`.
- Embedded continuity and registry: `<home>/aethermind`.
- Imported and newly created local cores: `<home>/aethermind-cores`.
- Default Autonomy state: `<home>/autonomy/state.json`.
- Shared autonomy definitions: `<application_home>/autonomy-templates/<id>.json`.
- Profile-owned setup packages: `<home>/autonomy/setups/<id>`, containing
  `setup.json`, `state.json`, `floor-definition.json`, `lessons.json` when present,
  task outputs, and the floor audit when present.
- Floor definition and audit: files under `<home>`.
- Sign-in status without tokens: `<home>/oauth/<provider>.json`.
- Default workspace: `<home>/workspace`.

Chat attachments retain validated host-file references, not independent payload
copies; their raw metadata includes source identity and checksum. Custom avatar
imports use separate app-owned normalized image storage.

The app and runtime call the manifest-named credential helper through private
stdin/stdout JSON, never secret command arguments. The native caller serializes
requests off the UI thread and drains both pipe directions under a deadline.
Writes include readback. Presence checks do not request secret data. Model keys use exact
endpoint-bound accounts; provider requests resolve the current item. Optional
model credentials, selected voice credentials and app-owned OAuth grants keep
separate ownership. No other client's grant is imported. Product identity,
services and provider defaults come from `product.json`.

Computer-use helpers are prebuilt and signed from the native-helper manifest.
The default runtime does not compile or rewrite its signed app bundle.
