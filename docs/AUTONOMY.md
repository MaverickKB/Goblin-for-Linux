# Autonomy

Goblin has one persistent session. Autonomy lets that session wake itself for
work the operator did not just type.

A wake is an ordinary turn whose input is an event digest. The digest names the
event, the evidence, and the pressure that made it worth a turn. The session
still uses its normal tools, approvals, log and continuity capture.

## Wake sources

- Timers set by the operator or by the session.
- Background tasks.
- Approval outcomes.
- Floor rules from the saved floor definition.
- Agenda items, tasks and host signals.

## Delivery

Events are held, not dropped. A wake waits until the session is idle and the
rate caps allow another turn. The state lives in the autonomy state file under
the app home directory. That file is product data, not source.

## Floor definition

The floor definition is the editable policy for what can wake the session.
`floor/read` returns it and `floor/save` writes it. The definition holds the
world view, pressure rules, host limits, the footer text and the wake score.
The default definition is built in. A saved definition wins over the default.

The floor signals are the live rows the rules read. They come from host
metrics, the session card, timers, agenda items, tasks and endpoints.

## One Goblin

This install has one Goblin and no shared work board. Floor rules that name
board cards are not part of this engine. The multi-session work-in-progress
board, crew and channel hops are out of scope.

## Approvals

A risky tool can ask the operator. The approval card is part of the chat. An
explicit per-tool grant, and the yolo or never policy, skip the card. Otherwise
the call waits for an approval.

Operator Stop fences autonomous wakes until a human continues. The autonomy
enabled switch is separate. Turning it off closes the worker without deleting
timers, tasks or approvals.

## Files

- `autonomy.py` owns the worker, timers, tasks, approvals and wakes.
- `floor_definition.py` validates and evaluates the saved floor.
- `floor_signals.py` builds the live rows the rules read.
- `learning.py` stores intentions and their lifecycle.
- `endpoints.py` holds endpoint health and due checks.
- `session.py` runs the turn.
