---
name: pstack-recall
description: "Reconstruct your recent working context from chat history, live state, and the shared record, then hand back a tight current-state brief. Use before starting or resuming work."
---

# Recall

**Before you start or resume work, rebuild the user's recent working context and hand back a tight capsule of where things stand and what to do next.**

Keep it tight and on-topic. Read only what the in-scope threads need, then stop.

Your context lives in two records. Your own chat history holds what you did and decided. The shared record holds everything that happened around the same code under other names: the symptoms users keep reporting, the fixes that shipped and got reverted, the errors still firing in production. That second record is what the `pstack-why` skill searches, across source control, the issue tracker, chat channels, long-form docs, and error tracking. A feature with a long bug tail keeps most of its story there, so don't rebuild it from your transcripts alone.

1. Classify, then route. One specific prior chat to resume is a session pickup, not this. Turning habits into a durable skill is a separate authoring job, not this. A human-readable summary of your work is a different task. Recall loads working context across recent work before you act. If the user already gave you a full state capsule (paths, branch, the change), use it and skip the mining.
2. Lock the scope before searching. Pin the window ("recent" is a real range, default the last 7 days), the topic if named, and the workspace (default the active one. Never read another project's records without being asked). State the scope back. Never quietly turn "all" into "recent N".
3. Sweep your own history. `history_search` for the topic first, then `history_read` or `history_zoom` around the hits, and read only the matching regions. Skip the current turn and obvious noise. For one or two threads, search directly. For a broad sweep, spawn one or two read-only workers with `history_search`, `history_read` and `history_zoom` only, one slice each, and have them return the same schema: topic, the user's goal, decisions, open threads, struggles and corrections, artifacts (commits, branches, cards), each citing the message index. The raw transcript stays in the workers. The main thread gets only their findings. Pick each worker's capability from `agent_backends` and keep it cheap.
4. Sweep the shared record whenever the topic names a feature, file, subsystem, area, or bug. This is the default, not a judgment call, and "my work on X" does not exempt it. `memory_recall` on the named target, and `memory_tree` when you need the shape of what is stored. Ask it what the current state is, what has been tried and did not hold, and what is still being reported, not only why it was built this way. Null results are findings; say so. Skip this step only for pure activity recall with no named target, where your own history and live state are the whole answer.
5. Verify against live state. Take the commits, branches and cards the sweep surfaced and check them for real: `git_read` for HEAD, status and log, `shell` for a live probe, the board or API for card status. When the answer hinges on what an agent actually did (the tools it ran, files it read, errors it hit), read the full record, not a trimmed summary.
6. Write the brief to the contract below. Group by thread. Stay on the named topic.

## Output contract

Lead with the capsule, then thread status, then problems, then the next move. Deeper detail goes below or gets cut.

- **Capsule.** At most 5 bullets. What this work is and where it stands overall.
- **Threads.** One line each, prefixed with exactly one status tag: `[merged <sha>]`, `[open PR <n>]`, `[in flight <branch>]`, `[verified, uncommitted]`, `[reverted <sha>]`, or `[planned, not started]`. A thread with no tag is not done yet, so tag it.
- **Problems.** At most 5, the recurring ones. Include the symptoms still being reported and any fix that shipped and was reverted, so the next attempt starts where the last one failed.
- **Next move.** The single most useful next action, concrete.

An adjacent feature or card stays out unless it blocks this one. When the capsule and thread lines outgrow a screen, cut detail before you cut threads. Cite chat findings by message index and shared-record findings by their source (commit, card, thread). Sanitize private context before any public output.

**Reply:** the brief, to the contract above. Write it through `pstack-unslop`.
