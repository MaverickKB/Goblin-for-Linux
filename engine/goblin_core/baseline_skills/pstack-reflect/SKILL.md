---
name: pstack-reflect
description: "Spawn three parallel review subagents over the active transcript, surface learnings, and route each to a concrete edit on an existing skill. Use when the user says reflect."
---

# Reflect

Mine the current conversation for durable learnings, then route them into skill edits.

Invoke when the operator says "reflect" or "/reflect". Skip when the conversation is trivial, off-topic, or already covered by an existing skill the parent followed correctly. One-offs are not learnings.

## 1. Locate the active transcript

Before fanning out, gather this session's transcript with the history tools: history_search to find the opening user prompt and key moments, history_read / history_zoom for the spans you need. Do not search other sessions' or projects' transcripts. That crosses workspace boundaries and reads private chats from unrelated work.

If the transcript is not retrievable, pass a tight digest of the session instead.

## 2. Spawn three reviewers in parallel

One message, three `spawn_agent` calls (capability standard for tooling, complex for judgment work). Reviewers may need web access (web_fetch) to look up the tickets, chat threads, and observability traces the transcript references.

Choose the cheapest capable worker via agent_backends; a reviewer needs judgment and web access, not the biggest model. Keep the lens-to-template mapping:

| Lens | Prompt template |
|---|---|
| Judgment | `references/judgment-reviewer.md` |
| Tooling | `references/tooling-reviewer.md` |
| Divergent | `references/divergent-reviewer.md` |

Pass each template verbatim, substituting the transcript digest or history excerpts where marked. Reviewer reports return to the chat via wait_agents.

## 3. Synthesize

One more `spawn_agent` (capability complex). It spot-verifies citations with `web_fetch`, or with `history_search`, `history_read`, and `history_zoom` over the active session where available. If its own worker history lacks the parent's cited span, pass that span with its original record indices; do not substitute an unrelated transcript. Pass `references/synthesizer.md` verbatim, with each reviewer's full report inlined where marked. It returns an Accepted / Rejected / Backlog list.

## 4. Structural enforcement check

Move any Accepted item that a lint rule, script, metadata flag, or runtime check would enforce more reliably to Backlog. See the **pstack-principle-encode-lessons-in-structure** principle skill.

## 5. Apply

Present the synthesizer's full Accepted / Rejected / Backlog output and wait for explicit approval before applying any Accepted edit. The operator picks the subset and may redirect routings. Skill changes affect every future agent in the org. Do not auto-apply.

File each Backlog item to your team's backlog tracker without waiting. Only the Accepted list waits for approval.

Skills live on the skill shelf (the Goblin skills directory); the user shelf is edited with skill_write, which writes a full SKILL.md with name/description frontmatter. Never edit a shelf skill without showing the destination first. Never open a PR or push.

Follow each approved row's Routing exactly:

- Trivial existing-skill edit (a one-line bullet, a tightened sentence, a stale fact corrected): the parent does it directly with skill_write (or edit_file on the shelf copy), keeping the rest of the file byte-identical.
- Substantive existing-skill edit (a new section, a new pattern table, more than ~10 lines): the parent rewrites the SKILL.md deliberately with skill_write, preserving the original voice and structure, then reports the diff to the operator.
- `tune description: <skill path>` (the skill exists but didn't trigger when it should have): rewrite the `description:` frontmatter line so it names the trigger phrases, with skill_write.
- `new skill: <kebab-name>`: author the full SKILL.md (frontmatter `name` equal to the directory name, one-line description when to use it) and write it with skill_write. Do not invent a different shape.

If your environment ships a SKILL.md validator, run it on every touched skill before declaring done.

## 6. Summarize for the operator

Short list, no preamble:

- Edits applied: `<skill path>`. What changed, one line each.
- New skills created: `<skill path>`. One line each (rare).
- Backlog filed to the tracker: `<issue title>` (`<tags>`). One line each.
- Dropped: one line per rejected finding + reason from the synthesizer.

Durable lessons surfaced by the run go to memory_note (type: lesson); decisions to type: decision.
