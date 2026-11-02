# User manual

Each Goblin is one continuing conversation on your Mac. Use it for direct
requests, or enable Autonomy for work that can continue between your messages.
Its workspace, tools, conversation history, and AetherMind belong to that
Goblin's session.

For the relationship between models, Gist, AetherMind, and Autonomy, read
[How Goblin works](concepts.md). For source changes and builds, read the
[Developer guide](developer-guide.md).

Open these manuals from **Help > Goblin Help**. The in-app viewer searches the
bundled documents. Goblin can consult the same text through `app_help`.

## Choose or create a Goblin

The app opens your default Goblin at launch. Open the name dropdown at the upper
left of chat to switch profiles, use **Open in new window**, or choose **Create
new Goblin…**. **File > Goblin profiles…** and **Goblin profiles…** in Settings
open the full picker. Enter a **New Goblin
name**, then choose **Create Goblin**. Select a Goblin and choose **Recall here**
to show it in the current window, or **Open in new window** to show it alongside
another Goblin. An already presented Goblin is focused in its existing window.

Window recall keeps the outgoing Goblin's work running in its retained runtime.
Each Goblin keeps its own model settings, workspace, personality, avatar,
companion preferences, histories, default memory core, skill shelf, and live
autonomy state. Edit its **Name** in Settings and save settings to rename it.
Saved endpoint keys and account credentials use the app's credential services;
a profile is not a separate credential account or operating-system sandbox.

Autonomy templates share reusable definitions. Their live execution remains
independent for each Goblin. If you want Goblins to use the same memory core,
explicitly attach that existing local or remote core in each Goblin's
**Continuity** settings. New Goblins start with their own default embedded core.

## Connect a model

1. Open **Settings**, then **Connections**.
2. Enter the local or custom server's API base URL and model identifier.
3. Select the protocol that the server exposes.
4. Save settings, then send a short message in chat.

An OpenAI-compatible connection uses the server's chat-completions interface.
A Responses API connection uses its responses interface. A CLI worker runs the
configured command. The model identifier must match the server or command.
Goblin does not start or download an inference server for you.

Local inference uses your machine or another server you control. Its capacity
still limits context size and concurrent requests. An external provider may
charge for model calls. Goblin's Autonomy control lets you decide whether it
can initiate further work between your messages.

In **Local or custom models**, choose **Add saved model**, or use **Save current
chat as a model**. Give each entry a recognizable name. **Use for chat** copies
the entry into the chat draft. **Use for compression** copies a supported HTTP
entry into the compression draft. Choose **Save settings** to apply the changes.
Compression can also inherit chat. CLI entries apply to chat. Credentials remain
separate from saved model entries.

For an account connection, connect the account and choose a model available to
that account. Account choices retain their own model settings. Switching to an
account preserves the custom endpoint configuration.

If a server needs a key, use the connection's key controls. Goblin stores model
keys in endpoint-scoped Keychain items. A local server that accepts anonymous
requests does not need a saved key. **Allow Keychain access** requests macOS
permission when a protected saved item needs it.

## Choose a workspace

Open **Settings**, then **Goblin**, and choose a workspace directory. File and
shell tools use that directory as their working location. Name the relevant
project or file in your request so Goblin can inspect the actual source.

Keep credentials in the appropriate credential store. A workspace description,
personality, and world view provide context. They do not configure SSH access,
create service connections, or grant tool permissions.

## Shape the personality

Use **Persona and instructions** to describe the role and working style you
want. **Create personality draft…** provides a starting format. You can edit
its result or write your own text.

A useful draft separates these ideas:

```markdown
## Role
Help me maintain the project in the selected workspace.

## Voice
Use concise, direct language. Explain technical terms when they matter.

## Working preferences
Inspect the relevant files before proposing a change.
Keep progress updates tied to actual findings.

## Boundaries and priorities
Preserve existing work and follow the task's stated scope.
```

These headings help creation. They are not a required runtime schema. Free
edits remain available. Instructions work best when they describe observable
behavior and give the model enough context to recognize success.

## Send, queue, steer, and stop

Type in the chat composer. **Return** sends when Goblin is idle.
**Shift+Return** inserts a line. While a turn runs, the composer shows whether
Return steers that turn or queues another message, according to your setting.
**Command+Return** uses the configured queue or steer action.

**Steer** adds direction to the current turn at a model boundary. **Queue**
preserves a message for a later turn. **Stop** cancels current work and records
the interruption. Stop also fences autonomous wakes until you continue.

Attach a file with the paperclip, or paste one into the composer. A pasted
screenshot becomes a PNG attachment. A file copied in Finder keeps its own path.
Up to six attachments travel with one turn, and image contents reach the model
for that turn only.

Accepted messages and steering are saved before Goblin acknowledges them.
After a disconnected engine or app restart, unfinished accepted input remains
visible as interrupted work. Goblin does not silently replay it.

Expand a reply or tool entry when you need details. Use earlier-history controls
to load older conversation pages. Saved records expose the content and metadata
as recorded. Ask Goblin to zoom into an earlier summary when you need its source
details rather than another summary.

### Start fresh or recall a history

Open **Settings > Continuity > Advanced: session history**. **Start fresh
history** starts a new transcript and Gist while retaining your personality,
settings, selected AetherMind core, and autonomy setup. Earlier histories remain
on disk. Choose one under **Retained history**, then choose **Recall selected
history** to continue it.

Save settings drafts before switching. Current turns, workers, background jobs,
voice sessions, and approvals finish first. These controls preserve work and do
not issue Stop. Background compression can finish against the retained history;
if that history is still finishing summaries, recall reports that condition.

## Set up autonomous work

1. Describe the continuing purpose and priorities in **World view**.
2. Review the floor definition and the signals it uses.
3. Configure the tools and connections needed for that work.
4. Start Autonomy when you want Goblin to initiate turns.

Use the autonomy control in chat or **Settings > Autonomy** to pause or start
event delivery. Pause preserves saved timers, tasks, and approvals. Use **Stop**
when you also want to cancel the current turn.

### Write the world view

World view is one plain-text note for this Goblin, up to 2,000 characters.
Goblin adds it only to floor wakes (pressure and the quiet reflection wake),
not to timer, task, approval, or agenda wakes. It does not grant access or hold
passwords.

**Create world view draft…** organizes a draft. You can freely edit its result.
A useful format is:

```markdown
## Purpose
Keep the selected project moving toward its accepted goals.

## Current priorities
Finish the work already accepted in chat and identify concrete blockers.

## Environment
Use the configured workspace and available connections.
Describe the role of any relevant machines or services here.

## Standing work
Check the evidence that matters to the task. Report meaningful changes.

## Success
Verify the requested outcome and preserve unresolved work clearly.
```

List machine and service names when their roles help the task. Configure actual
remote access through tool connections. A world view does not need passwords,
SSH private keys, or API tokens.

### Edit the floor

The floor definition controls how measured signals become pressure and wake
text. Goblin includes a default definition. Read the current definition before
editing so you retain its existing rules and composition fields.

A new Goblin has no assigned monitoring job. Its default floor has no rules
or scripted assignments, and it never samples CPU, memory, disk, or process
metrics on its own. Give the Goblin its task in World view and let each floor
session use its configured tools to check the relevant state and act on it.

For example: "Watch new PRs in my project, fix small bugs, and merge only after
CI passes for the latest commit. Keep tests and workflows intact. Ask me about
risky changes." A floor session can perform that check without creating a
session timer. Timers and background tasks remain available for tasks that
actually need them.

A rule can also choose `stand_down`. When its condition matches, autonomy turns
off by itself, which is how a completion condition stops the loop after a task
or a deadline. The on switch stays yours: nothing except this Settings page can
start autonomy.

Rules read four signals: `timer` (this session's wake timers), `task`
(background tasks the Goblin started), `agenda_item` (its open lessons,
questions and intentions), and `notice` (readings the floor failed to take).
For example, `signal: task` with `when: "state == 'done'"` stands down when the
task it was given finishes. A timer rule sees a due timer before the timer
fires, so a deadline is expressible.

Only pressure rules are held when their score is below `compose.wake_score`
(default 1.0). A rule with pressure turned off is not checked against that
score.

While Autonomy is on and `standing_floor` is on (the default), the worker
checks about every second and composes an idle floor wake when the Floor
interval has elapsed. **Floor interval (seconds)** writes
`floor_interval_seconds`. That value becomes `self_prompt_gap_seconds` unless
the expert autonomy object sets the gap itself. The default interval is 900
seconds. Pressure wakes are not held for that interval.
Delivered wakes are also held for
`advanced.autonomy.min_wake_gap_seconds` (default 60).
`advanced.autonomy.max_wakes_per_hour` and `max_wakes_per_day` default to 0,
which means no cap. Set either to a positive number to block further wakes
until that hour or day window passes. Set `standing_floor` to false to stop
floor wakes. `self_prompt_enabled` (default true) controls agenda
self-prompts, not the idle floor wake.

Opening the floor disclosure reads its current definition. Guided controls
let you enable existing rules and edit **Quiet-time guidance**, preserving
the rest of the definition.

The saved floor file is JSON (`floor-definition.json`, or
`floor-definition-<profile>.json`). The expert editor accepts JSON or TOML.
TOML is converted on import. **Read floor** and the file on disk are JSON.
**Apply definition** writes that JSON. Malformed or unsupported data reports
the affected field. The engine keeps its existing functional validation.

The floor is not the generated `autonomy/state.json` file. That runtime file
records timers, held events, tasks, and other live state. Describe desired
behavior in the floor definition instead of editing generated bookkeeping.

**Prepare setup in chat** fills the composer with a setup request. Review and
send it when you want Goblin's help. Preparing the request does not send, steer,
or stop a running turn. Review the proposed behavior and resulting editable
text. Assisted creation uses a guide. Manual edits do not acquire an additional
contract requirement.

### Save and recall autonomy setups

Under **Advanced: autonomy setups and reset**, save your settings edits first.
Enter a **Template name**, then choose **Save saved settings as template**.
This copies the saved autonomy configuration, effective World View, complete
floor, and current schedule definitions into the shared template catalog.
Unsaved fields are not included.

Choose a **Saved template or setup**, then **Recall selected**. Each Goblin gets
its own live instance of a template. Its timers, queued wakes, task history,
learning, outputs, and floor audit belong to that instance. Returning to a
previously used setup restores that Goblin's existing state. Another Goblin's
execution state is never copied with a shared template. Editing a live setup
does not automatically rewrite its saved template or other Goblins' setups.
Timers retain their recorded due times while a setup is away. An enabled setup
resumes its enabled behavior when recalled; the normal Autonomy control remains
available when you want it paused.

**Reset to defaults** creates a setup with the shipped autonomy settings and
floor. It retains the outgoing setup for recall. Your personality, models,
selected memory core, and conversation history remain in place. Running jobs
and approvals finish before recall or reset. A held operator Stop remains held.

## Choose tools and integrations

Open **Settings > Tools** to review registered tools and their descriptions.
Under **Tool access**, enable or disable individual tools, then save settings.
Disabling a tool removes it from the available tool set and prevents execution
through the registry.
Re-enable it from the same list.

**Pause acting tools** prepares a draft that disables currently registered tools
marked risky or assigned to the acting class. It leaves the approval mode and
automatic memory capture unchanged. Review the choices, then save settings.
**Enable all registered tools** re-enables the registered tools in that draft.
New registrations need their own review. These controls do not create a
read-only sandbox; remaining tools can still read files or make web requests.

Add external tool providers under **Tool integrations**. Use the provider's HTTP
endpoint or its local standard-input and standard-output command. The command
field uses a JSON array of executable and argument strings. Removing an
integration removes that provider's registration. Built-in tools remain part
of Goblin and can be disabled individually.

For SSH tools, configure a named entry in the expert configuration's `remotes`
object. Establish the host's SSH access and known-host entry with your SSH
client first. Goblin uses noninteractive SSH. An optional `identity_file` names
an existing key file; the world view never contains that key's contents.

Goblin is a power tool for operators who choose its workspace, credentials,
connections, and access. **Ask when needed** uses the existing approval rules.
**Unleashed** runs enabled tools without approval prompts. These controls are
not a sandbox: tools act with the permissions available to the process. You can
disable acting tools for read-focused work or use an external isolated
environment when you want containment. Those are your configuration choices;
Goblin does not require a sandbox to run Unleashed.

Web pages, tool results, and imported memories carry claims from their sources.
A quoted instruction does not grant user authority, and a memory's assertion
about your preferences is not proof that you expressed it. Review source and
context when those claims matter. Your instructions and configured tool access
remain the basis for authorized work.

Goblin carries source labels with model inputs, worker reports, and new Gist
summaries. The saved transcript text remains unchanged. These labels help
attribute claims; they do not authenticate imported memory or guarantee that
a model interprets every source correctly.

The pstack operating core is already present in each turn. You do not need to
invoke it by name. Goblin selects detailed procedures from their triggers and
available tools. Optional skill retrieval settings do not remove the governing
core.

### Use the expert editor

**Expert: full runtime configuration** exposes the advanced settings object.
It contains integration, tool, autonomy, and compression settings. It is useful
for fields that do not have a dedicated control.

**Validate JSON** checks the draft without reformatting it. **Format JSON** is
an explicit text replacement. **Save settings** validates supported fields and
applies the configuration.

A saved compression-model value can remain in this object while another route
is active. It does not mean an extra agent is running. Use the effective model
shown in **Compression** to identify the route Goblin will use.

## Use AetherMind

Goblin uses an embedded AetherMind core by default. It captures durable
experience, decisions, corrections, and unresolved work separately from the
conversation's Gist history.

The default core starts with general knowledge of Goblin's tools, documentation,
runtime, and working practices. That foundation is available from the first
request. Your personality, preferences, and shared history come from your
settings and real interactions.

Startup preserves existing memories and custom skill directories. It adds only
missing foundation content. If setup cannot complete, **Continuity** shows the
foundation warning while preserving the existing store for diagnosis. Explicit
local attachments, imported copies, and remote cores keep their own contents
without automatic reseeding.

Open **Settings > Continuity**, then expand **AetherMind core connection**.

To use a local core:

1. Select **Embedded/local core**.
2. Leave **Local project folder** blank for Goblin's default store, or choose
   the project folder containing an existing `.aethermind` directory.
3. Choose **Inspect compatibility** to inspect that draft connection.
4. Choose **Save settings** to apply it.

An explicitly selected folder must contain a readable, compatible core.
Goblin does not initialize a new store at a mistyped attachment path. Leaving
the local folder blank retains the default embedded-core setup.

To attach a remote core, select **Existing remote core (MCP)**. Enter its MCP
endpoint and the **Remote project root or key**. The path or key belongs to
the server that owns the store. A path on your Mac does not identify a directory
on another machine. If the server needs a bearer credential, the optional token
environment variable names the variable available to Goblin's engine. It does
not contain the token itself. Inspect the connection, then save settings.

Inspection does not activate the draft. A connection saved during an active
turn takes effect at the next idle boundary, preserving that turn's memory
target and inheritance.

An attached core receives Goblin's normal memory captures. To continue from
existing memories without writing to the source store, import a backup copy.

**Export active local core** writes a portable archive of the active local
store. **Import core backup** prepares a separate local copy and inspects it.
**Use imported copy** selects that copy in the settings draft. Save settings
to activate it. The original store and source archive remain unchanged. The
imported copy continues independently; changes are not synchronized back to
the source.

The archive contains the core's native memory contents. Conversation history,
workspace files, settings, and Keychain credentials are separate app data.

Compatibility inspection distinguishes authored memory from derived summary
data. Supported adaptations operate on a copy. Unsupported formats are reported
with their remaining requirements. Goblin does not invent replacement memories
or promise to understand every future core version.

Remote connections use the remote server's existing MCP capabilities. Goblin's
local archive controls do not create a remote backup service.

### Create a separate core

Expand **Advanced core rotation**. **Create core with generic foundations**
prepares a new store with Goblin's general starting knowledge. **Create clean
empty core** prepares a store without those memories. Both select the new path
in the settings draft. Choose **Save settings** to activate it. The previous
core remains available through its local project folder.

Core rotation changes memory. It keeps conversation history, autonomy setups,
and the skill shelf. An empty core still has Goblin's normal tools, pstack
operating baseline, and bundled manuals. It can learn from subsequent work.

## Use voice and the companion window

Connect your xAI voice account or save a voice API key in **Connections**.
Microphone permission is requested when voice starts. Use headphones, or
explicitly enable the built-in speakers.

With hold-to-talk enabled, holding the character opens the microphone. Release
mutes the microphone while preserving playback. Double-click opens chat.

Collapse the chat for short replies. Drag the character, or the empty area in the chat header, to
move the window. Configure always-on-top, motion, and other window options in
Settings. Reduce Motion uses the rest pose.

Import a custom avatar in Settings or from the character's context menu.
Transparent images work best. Goblin normalizes the image and stores its own
copy, so moving the original file does not remove the imported avatar.

## Troubleshoot a connection or setting

- **No model is ready:** check the active connection, model identifier, endpoint,
  and account selection. A blank model is a valid incomplete setup.
- **Keychain permission is required:** use the explicit access control for that
  connection. A denied read does not remove the saved item.
- **The engine disconnected:** reconnect and inspect preserved input and partial
  replies before deciding which work to resume.
- **Settings changed elsewhere:** reload the saved revision, review the values,
  and apply your changes again.
- **A floor edit fails:** read the reported rule and field. Compare your draft
  with the complete current definition.
- **Advanced JSON fails:** correct the reported syntax or field. Formatting is
  an explicit editing action, separate from validation and saving.
- **A core is incompatible:** preserve the source and read the inspection
  report. Use a compatible core or a supported conversion on a copy.

## Ask Goblin to help modify Goblin

Goblin's `app_help` tool can list, search, and read the same manuals shown in
Help. Ask it to read the Developer guide and inspect a source checkout in
the selected workspace. The installed app bundle is the running artifact.
Build changes from source and verify the replacement before installing it.
Preserve current work when choosing when to restart the app.
