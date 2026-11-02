# How Goblin works

Goblin is a Mac app with one continuing, tool-using session for each profile.
A model supplies inference. The app owns the conversation, settings, tool
permissions, pending work, and lifecycle. A model connection is one part of
that arrangement.

## The native app and local engine

SwiftUI and AppKit present the character, chat, settings, approvals, and native
device controls. A bundled Python engine owns the session and runtime tools.
The native app retains one private engine for each loaded Goblin profile.
The app and engine exchange JSON over private standard-input and standard-output
pipes. Goblin opens no local listening server.

Model servers and optional tool services remain explicit client connections.
The embedded memory core works in process. Another agent application is not
required to run Goblin.

A profile owns its settings, conversation histories, default embedded memory,
avatar, companion preferences, custom skill shelf, and autonomy execution.
Recalling another profile in a window changes presentation while the outgoing
runtime remains retained. An already presented profile uses its existing
window. The default profile keeps its original storage and preference identity.
The shared application root owns the profile catalog and reusable autonomy
templates. Explicit connections can share an existing AetherMind core. Product
credential services remain app-wide.

## Models and continuing work

Chat uses the selected custom endpoint, CLI worker, or connected account model.
Saved custom model entries let the same installation retain several routes.
Compression can inherit chat or use a separately selected HTTP connection.

Autonomy increases the opportunity for model calls because the session can
initiate work between human messages. A local model avoids a provider's
per-request API bill, while using the serving machine's compute and memory.
An external provider applies its own usage and billing terms. Pausing Autonomy
lets you control self-initiated work without removing the session's context.

## The transcript, Gist, and zoom

The transcript is the append-only record of what Goblin saves from conversation
and tool activity. Gist is the bounded history view supplied to the model.
Summary nodes represent ranges of earlier records in a binary tree. The view
can replace older ranges with summaries while retaining access to their saved
source.

Each retained history owns its transcript, summary tree, and saved view.
Starting fresh selects another history without replacing the selected memory
core, personality, settings, or autonomy setup. Recalling a history restores
that history's context. Its memory captures continue to use the explicitly
selected core.

Zoom expands a summary range. It provides the recorded detail behind that
summary, with bounded pages for larger ranges. This preserves a way to verify
earlier decisions and tool results without placing the whole transcript in
every prompt.

Gist compression is a background activity. At the start of a new turn, Goblin
waits for the complete history prefix that turn needs. It captures that view
once. Model calls within the active turn then append tool calls, results, and
accepted steering to the same chain.

Background workers can finish immutable summaries while a turn runs. Goblin
publishes coarser history views between turns. It does not pause an active tool
loop to replace its history prefix with newly compressed context.

Stable prefixes avoid unnecessary changes to content that a serving engine may
reuse. Engines differ in how they retain and evict caches. Goblin's design does
not depend on a universal server cache policy or claim proof of eviction from
application behavior alone.

Model inputs carry Goblin-owned source labels for direct user input, tool data,
worker reports, and other origins. New summary nodes retain bounded lineage
metadata while saved transcript text remains unchanged. A source label helps
attribute a claim. It neither proves that claim nor gives imported memory or
quoted instructions authority over the user's actual request.

## AetherMind and durable experience

AetherMind is a continuity store for experience that should shape future work.
Its native layers retain bodies, context, markers, relationships, and provenance.
Corrections, supersession, rollback, and unresolved debt carry distinct meanings.
Authored texture is retained separately from factual layers.

Goblin prepares relevant inheritance for its work and captures outcomes through
a durable write outbox. A pending write remains debt until acknowledged. A
receipt that data was delivered does not prove that a model understood or used
it.

Gist and AetherMind serve different jobs. Gist bounds the model's conversation
history and supports expansion into recorded source. AetherMind carries durable
experience and its correction relationships. AetherMind is not a replacement
for exact conversation history, and a conversation summary is not a complete
memory-core backup.

The embedded core is Goblin's default. An advanced connection can attach an
existing core through the interface and project identity of its owning host.
This allows a focused project companion or a companion using a broader existing
store. The selected store remains an explicit ownership choice.

The default app-owned core begins with generic product and operating knowledge.
It covers available capabilities, finding bundled help, observing the
environment, and verifying work. General procedures are staged on the skill
shelf. These defaults supply a starting reference. The user's settings and
interactions establish personality, preferences, and shared experience.

Bootstrap runs before initial capture and inheritance. It appends missing
native memory bodies using content hashes, retains existing records and custom
procedures, and reports setup failures without resetting the store. An explicit
local attachment, imported copy, or remote core retains its existing contents
and receives no automatic foundation pack.

## Core portability and compatibility

A local core archive preserves native store bytes and a file manifest.
Recreating memory by replaying selected `read_layers` results would lose the
guarantee of retaining every native field, relationship, and authored texture.

Import therefore prepares a separate copy. Inspection checks the copied store
against the bundled implementation. Known adaptations can operate on that copy
while leaving the original intact. A runtime version, adapter contract, and
storage format are different identities. A version number alone does not prove
that two stores are interchangeable.

Derived summary data also has a different role from authored memory. If a tree
needs work, inspection can report that requirement without inventing summaries
or rewriting the source memories. Unsupported conversions remain explicit.
Local file portability does not imply that a remote MCP service exposes an
archive transfer operation.

Creating a seeded or empty local core prepares a new independent store. Saving
its connection activates it at the normal memory boundary. The old core remains
retained. Core creation does not rotate conversation history or modify the
skill shelf. An empty memory store and an empty tool or instruction set are
different things: the operating baseline and bundled help remain available.

## Personality, world view, floor, and world state

Personality describes the companion's role, voice, and working preferences.
World view explains the purpose and priorities used to interpret autonomous
observations. Both remain editable prose. An assisted creator supplies a useful
format without making that format mandatory for later edits.

The floor definition is executable configuration. It specifies rules over
available signals, scores pressure, and composes wake text. The fresh definition assigns no monitoring job or host-management policy.
The native runtime does not automatically sample local resource metrics. The saved file is
JSON. The editor also accepts TOML, which is stored as JSON. The generated
runtime state records timers, tasks, held events, approvals, and related
bookkeeping.

World view is one note for the Goblin. It is included in floor wakes only.
Machine and service names in that note do not create connections. Access comes
from configured tools.

## Autonomy uses the same session

Autonomy can initiate a turn from timers, background task outcomes, floor
pressure rules, and approval events. A wake carries an event digest through the
same session, history, tools, approvals, and AetherMind capture as a typed
request.

A floor rule may instead stand the Goblin down: when its condition matches,
autonomy turns off and stays off until the operator starts it again from
Settings. Starting autonomy is never the Goblin's own decision.

Events can remain held until the session is idle and delivery conditions permit
another turn. Pausing Autonomy preserves its saved state. Operator Stop is a
separate cancellation and wake-fencing action.

Autonomy templates share reusable behavior, World View, the full floor, and
schedule definitions. Live execution belongs to each Goblin's setup instance.
That includes timer identities and due times, queued wakes, tasks, learning,
outputs, and floor audit. Returning to an existing setup restores its own saved
state. First use instantiates its schedule definitions with independent timer
identities and the recorded due times; ordinary overdue-timer behavior applies.
Changes to a live setup do not propagate into other Goblins or saved templates.

Recall and reset require running work and approvals to finish. Reset uses the
shipped autonomy defaults and retains the outgoing package. It changes neither
conversation history nor the selected core, and preserves operator Stop.

## Tools and the operating baseline

The tool registry owns available capabilities and approval behavior. Optional
MCP integrations contribute tools through configured providers. Disabling a
tool affects both model discovery and execution through the registry.

The pstack operating core and complete effective skill catalog are governing
instructions outside the aging conversation log. Each root or worker turn gets
them automatically. User instructions, actual tool permissions, and concurrency
limits govern their defaults. Detailed procedures are selected by their
substantive triggers.

Goblin captures the instruction prefix at turn admission. Skill or configuration
changes affect a subsequent turn rather than rewriting the active tool chain.
Compression requests retain their source-only summary role and do not execute
the workflows described in the operating core.
