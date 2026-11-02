"""One model seat, with foreground priority and resumable background work."""
from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import dataclass
import threading


class ModelYielded(RuntimeError):
    pass


def model_route_key(binding):
    # Reasoning and token budgets are request settings, not seat ownership.
    return binding[:6]


class ModelCancellation:
    def __init__(self, stopped, preempted):
        self.stopped = stopped
        self.preempted = preempted

    def is_set(self):
        if self.preempted.is_set():
            return True
        return self.stopped is not None and self.stopped.is_set()


@dataclass
class _Lease:
    owner: object
    background: bool
    cancel: Callable[[], object]
    preempted: threading.Event


class ModelSeat:
    def __init__(self):
        self._condition = threading.Condition()
        self._active = None
        self._foreground_waiters = 0

    @contextmanager
    def use(self, owner, cancel, *, background=False, stopped=None):
        preempted = threading.Event()
        with self._condition:
            if not background:
                self._foreground_waiters += 1
            try:
                if not background and self._active is not None and self._active.background:
                    self._active.preempted.set()
                    self._active.cancel()
                while self._active is not None or (background and self._foreground_waiters):
                    if stopped is not None and stopped.is_set():
                        raise ModelYielded("Model work stopped before admission")
                    self._condition.wait(0.1)
                if stopped is not None and stopped.is_set():
                    raise ModelYielded("Model work stopped before admission")
                self._active = _Lease(owner, background, cancel, preempted)
            finally:
                if not background:
                    self._foreground_waiters -= 1
                self._condition.notify_all()
        try:
            yield preempted
        finally:
            with self._condition:
                self._active = None
                self._condition.notify_all()

    def cancel(self, owner):
        with self._condition:
            if self._active is not None and self._active.owner is owner:
                self._active.preempted.set()
                self._active.cancel()


class ModelClientPool:
    """Bound concurrent requests and keep each transport exclusively owned."""

    def __init__(self, factory):
        self._factory = factory
        self._condition = threading.Condition()
        self._active = {}
        self._idle = {}

    @contextmanager
    def use(self, binding, limit, stopped=None):
        route = model_route_key(binding)
        with self._condition:
            while self._active.get(route, 0) >= limit:
                if stopped is not None and stopped.is_set():
                    raise ModelYielded("Model work stopped before admission")
                self._condition.wait(0.1)
            if stopped is not None and stopped.is_set():
                raise ModelYielded("Model work stopped before admission")
            self._active[route] = self._active.get(route, 0) + 1
            idle = self._idle.setdefault(binding, [])
            client = idle.pop() if idle else None
        try:
            if client is None:
                client = self._factory(binding)
            yield client
        finally:
            with self._condition:
                if client is not None:
                    self._idle[binding].append(client)
                self._active[route] -= 1
                self._condition.notify_all()
