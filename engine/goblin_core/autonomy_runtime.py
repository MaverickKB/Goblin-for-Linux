"""Single-session runtime surface for Autonomy.

Goblin has one user-visible chat that is also the floor. This adapter gives
Autonomy the one session that GoblinHost owns.
"""

from __future__ import annotations

import threading


class SessionRuntimeProxy:
    """The fields Autonomy handlers read on a live runtime."""

    def __init__(self, host, session):
        self.host = host
        self.session = session
        self.slug = host.slug
        self.active = {"hop": 0}
        self.is_goblin = True
        self.goblin_id = host.slug

    @property
    def operator_stopped(self):
        return bool(getattr(self.host, "operator_stopped", False))

    def idle(self):
        if self.host.busy():
            return False
        if self.session._in_turn.is_set():
            return False
        if self.session.agents.active():
            return False
        return True

    def enqueue(self, text, sender=None, hop=0, work_id=None, *, turn_id=None,
                wake=None, source=None, attachments=None, project_id=None, selected=None):
        meta = {}
        if wake is not None:
            meta["wake"] = wake
        if self.operator_stopped:
            raise RuntimeError("Turn was fenced by operator Stop")
        return self.host.submit(
            self.slug,
            text,
            source=source or "autonomy",
            client_request_id=turn_id,
            turn_meta=meta,
            attachments=attachments,
        )


class AutonomyManagerAdapter:
    """Minimal SessionManager stand-in owned by GoblinHost."""

    def __init__(self, host):
        self.host = host
        self.config = host.config
        self._metadata = {host.slug: {"title": host.config.agent_name, "archived": False}}
        self._runtimes = {}
        self._lock = threading.RLock()

    def refresh_config(self):
        self.config = self.host.config
        self._metadata[self.host.slug] = {
            "title": self.host.config.agent_name,
            "archived": False,
        }

    def path(self, slug):
        if slug != self.host.slug:
            raise KeyError(slug)
        return self.host.session.root

    def ensure(self, slug, title):
        if slug != self.host.slug:
            raise KeyError("Single-Goblin install only owns session " + self.host.slug)
        self._metadata[slug] = {"title": title or self.host.config.agent_name, "archived": False}
        return self.runtime(slug, internal=True)

    def runtime(self, slug, internal=False):
        if slug != self.host.slug:
            raise KeyError(slug)
        proxy = SessionRuntimeProxy(self.host, self.host.session)
        self._runtimes[slug] = proxy
        return proxy

    def approval_status(self):
        return {"pending": self.host.pending_approvals()}
