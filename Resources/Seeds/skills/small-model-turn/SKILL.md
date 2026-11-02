---
name: small-model-turn
description: Use when you are running on a small local model, or when a turn is drifting into long confident prose with nothing measured behind it. Supports evidence-based next actions and clear claims at the depth the user requests.
---

# Small model turn

A small model can do excellent work in this harness. What it cannot afford is
invention. The failure mode is a long, fluent answer built from assumptions,
which reads well and is wrong.

## The shape of useful work

1. **Choose deciding evidence.** Identify what observation would change your next decision.
2. **Read the result.** Use actual outputs to decide what follows.
3. **Choose the next causal action.** Keep pursuing the user’s requested outcome while new evidence supports progress.
4. **Support consequential claims.** Preserve exact failure details and provide the useful evidence at the requested level of detail.

## Keep decisions evidence-based

Use the observed result to choose the next causal action. Repeated failure without
new evidence calls for a different approach. Name an exact missing verification
rather than filling the gap with a plausible claim. The user’s requested scope
and response depth determine the size of the work and explanation.

## Writing the reply

At the depth requested by the user, name what you observed, what you inferred, and what you still
do not know, in that order, if the order matters. A confident paragraph with
no probe behind it is worse than three lines that say what you actually saw.

## When you are not sure

Say so, and make the uncertainty specific: "the route answers, but I have not
seen the page read it" is usable. "It should work" is not.
