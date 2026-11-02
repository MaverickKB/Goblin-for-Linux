---
name: pstack-no-comments
description: "Run a comment audit on a diff or set of files, fix the accepted findings at the root, and offer encodings for claimed constraints."
---

# No comments

Audit the comments in scope, act on what survives review.

Defer to the auditor's fresh perspective.

## Scope

Use the caller's files or diff. Otherwise use the current diff against the base branch, default `main`, including the working tree. Read it with `git_read` (`status`, `diff`, `log`).

## Steps

1. Read [`references/comment-auditor.md`](references/comment-auditor.md) with `skill_read`, then pass that procedure verbatim and the scope to one read-only auditor with `spawn_agent`. Pick its capability from `agent_backends`: `standard` for a normal diff, `complex` for a large or subtle one. Give it `shell`, `read_file`, `grep`, `glob`, `git_read`. The auditor reports; the parent applies accepted changes.
2. Inspect its report and diff. Reject application-code edits, scope escapes, exception-protected deletions, misstated `MUST KILL` reasons, and flags that treat kept intentional code as guilty. Reshape flags on our-code surprises stay actionable. Do not restore those comments. A keep survives only with proof it is about something we cannot change. Audit missed scoped lint and type suppressions. Correctness or safety suppressions stay actionable `MUST KILL`s. Restore deletions only with exact exceptions and scoped proof. Before accepting thin `IMPORTANT` or `do not remove` kills or keeps, load `pstack-how` or `pstack-why` on their symbol. If a kill is ambiguous, do not restore it. If a keep is refuted or still ambiguous, delete it. Revert and rerun one rejected report with the failure named. Reject a second, report it open, and fail the run.
3. Fix trivial accepted flags directly: delete a dead path, drop a parameter, use the real API. If any fix needs a shape, load `pstack-architect` once for the accepted set and surrounding code. Stop at the sketch. Architect shapes. Step 4 implements.
4. Implement the smallest root-cause fix in scope. Remove every named workaround. If the root cause is out of scope, land the smallest in-scope fix and report the rest open. The `pstack-principle-fix-root-causes` and `pstack-principle-redesign-from-first-principles` skills guide intent only. Neither authorizes widening the fence nor fixing instances outside it. Never bolt on symptom guards.
5. Constraint comments say `do not remove`, `do not change wording`, or `talk to X before changing`. Leave keeps about things we cannot change. Offer the cheapest in-scope type, runtime, test, or CI lint. Wait for interactive approval. Unattended work requires the caller to have pre-approved. If approved, encode then delete. Otherwise delete, report the constraint open, and sketch out-of-scope work.
6. Report the deletion count, restored comments, reruns, architect sketch, fixes, encoding offers, encodings, unenforced constraints, and other open work.

Run every response through `pstack-unslop`.
