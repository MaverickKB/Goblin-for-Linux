"""Internal background workers for one Goblin.

Workers are hidden helper sessions. They never appear as a second user-visible
Goblin, never own the Autonomy floor, and return their reports to the one chat.
"""

import re
import threading
import time
import uuid

from .provenance import source


class AgentManager:
    def __init__(self, session):
        self.session = session
        self._threads = {}
        self._reports = []
        self._lock = threading.RLock()

    def spawn(self, name, task, tools=None, backend=None, capability="routine", repo=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name or ""):
            raise ValueError("Agent name must be 1..64 letters, numbers, underscores, or hyphens")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("Agent task cannot be empty")
        parent = self.session
        if parent.depth >= parent.config.agents.max_depth:
            raise RuntimeError("Subagent depth limit reached")
        if backend is not None and backend != "default":
            raise ValueError("Unknown worker model: " + str(backend))
        if tools is not None:
            parent.tools.restricted(tools)
        ceiling = parent.tool_ceiling if parent.tool_ceiling else set(parent.tools.names())
        inherited_context = parent.view.render()
        parent_capture = parent.capture
        with self._lock:
            if self.active() >= 4:
                raise RuntimeError("Internal worker limit reached; wait for a report")
            worker_id = name + "-" + uuid.uuid4().hex[:8]
            cancelled = threading.Event()
            thread = threading.Thread(target=self._run,
                                      args=(worker_id, name, task.strip(), tools, ceiling, cancelled, repo, inherited_context, parent_capture),
                                      name="goblin-worker-" + worker_id, daemon=True)
            self._threads[worker_id] = {"thread": thread, "name": name, "cancelled": cancelled,
                                        "session": None, "started": time.time(), "report": None}
            thread.start()
        return {"agent": name, "id": worker_id, "status": "started"}

    def _run(self, worker_id, name, task, tools, ceiling, cancelled, repo, inherited_context, parent_capture):
        from .capture import Capture
        from .session import GoblinSession
        parent = self.session
        root = parent.root / "agents" / worker_id
        root.parent.mkdir(parents=True, exist_ok=True)
        config = parent._config_provider()
        capture = Capture(parent_capture.client, root / "outbox", parent_capture.project_id,
                          parent_capture.personal_project, "goblin-agent",
                          note_project=parent_capture.note_project)
        workspace = str(repo) if repo else config.workspace
        session = None
        try:
            session = GoblinSession(lambda: config, parent._backend_provider, root,
                                    capture=capture, depth=parent.depth + 1,
                                    allowed_tools=tools, tool_ceiling=ceiling,
                                    model_seat=parent.model_seat, approval=parent._approve,
                                    inherited_context=inherited_context, parent_view=parent.view,
                                    compactor_backend_factory=parent._compactor_backend_factory,
                                    compactor_client_lease=parent._compactor_client_lease)
            if repo:
                session.set_workspace(workspace)
            with self._lock:
                self._threads[worker_id]["session"] = session
            reply = session.run_turn(task, {"agent": name, "worker": worker_id},
                                     is_cancelled=cancelled.is_set)
            if session.last_turn_interrupted:
                outcome = "interrupted"
            elif session.last_turn_success:
                outcome = "completed"
            else:
                outcome = "failed"
        except Exception as error:
            reply = "Worker failed (" + type(error).__name__ + "): " + str(error)
            outcome = "failed"
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass
        provenance = source("worker_report", worker_id=worker_id, parent_session=parent.session_id,
                            worker_session=session.session_id if session is not None else "", outcome=outcome)
        report = {"provenance": provenance, "id": worker_id, "name": name, "task": task, "outcome": outcome, "report": reply}
        message = parent.log.append("work", "[" + name + "] " + reply,
                                    {"work_id": worker_id, "agent": name,
                                     "success": outcome == "completed", "provenance": provenance})
        report["record_index"] = message.i
        with self._lock:
            self._threads[worker_id]["report"] = report
            self._reports.append((message.i, report))

    def active(self):
        with self._lock:
            return sum(1 for item in self._threads.values() if item["thread"].is_alive())

    def active_details(self):
        with self._lock:
            return [{"id": key, "name": item["name"], "started": item["started"]}
                    for key, item in self._threads.items() if item["thread"].is_alive()]

    def cancel(self, reason="user"):
        with self._lock:
            items = list(self._threads.values())
        for item in items:
            item["cancelled"].set()
            session = item.get("session")
            if session is not None:
                try:
                    session.interrupt("user")
                except Exception:
                    pass

    def wait(self, timeout=None):
        with self._lock:
            threads = [item["thread"] for item in self._threads.values()]
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        for thread in threads:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            thread.join(remaining)

    def wait_for_reports(self, timeout=None):
        self.wait(timeout)
        reports = self.claim_reports()
        for report in reports:
            self.session.capture.record_source_evidence(report['provenance'], report['record_index'])
        return {"reports": reports, "active": self.active()}

    def claim_reports(self, before=0):
        """Claim each report once, excluding reports already covered by a gist."""
        with self._lock:
            pending, self._reports = self._reports, []
        reports = []
        for log_index, report in pending:
            if log_index >= before:
                reports.append(report)
        return reports

    def close(self):
        self.cancel()
        self.wait(5)
