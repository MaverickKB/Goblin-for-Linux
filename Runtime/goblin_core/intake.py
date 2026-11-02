"""Durable journal for accepted chat input that has not reached the chat log.

A submitted turn is written here, fsynced, and kept until the authoritative
user row exists in the ChatLog. An entry left behind by an abrupt process exit
is recovered as canceled input instead of being replayed.
"""

import json
from copy import deepcopy
import os
from pathlib import Path
import threading
import uuid

from .durability import sync_directory


JOURNAL_VERSION = 1


class IntakeJournal:
    """Atomic pending-intake file owned by the Goblin home."""

    def __init__(self, home):
        self.root = Path(home) / "intake"
        self.path = self.root / "pending.json"
        self._lock = threading.RLock()
        self.recovery_notice = ""
        self._entries = self._read()

    def _read(self):
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._quarantine()
            return []
        if not isinstance(payload, dict) or payload.get("version") != JOURNAL_VERSION:
            self._quarantine()
            return []
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            self._quarantine()
            return []
        entries = []
        invalid_entry = False
        for entry in raw_entries:
            if self._valid(entry):
                entries.append(entry)
            else:
                invalid_entry = True
        if invalid_entry:
            self._quarantine()
            self._write(entries)
        return entries

    @staticmethod
    def _valid(entry):
        if not isinstance(entry, dict):
            return False
        if not isinstance(entry.get("id"), str) or not entry["id"]:
            return False
        if not isinstance(entry.get("text"), str):
            return False
        if not isinstance(entry.get("source"), str):
            return False
        if not isinstance(entry.get("meta"), dict):
            return False
        return True

    def _quarantine(self):
        target = self.path.with_name(self.path.name + ".corrupt-" + uuid.uuid4().hex)
        os.replace(self.path, target)
        sync_directory(self.root)
        self.recovery_notice = "An unreadable intake journal was preserved at " + str(target)

    def entries(self):
        with self._lock:
            copied = []
            for entry in self._entries:
                copied.append(deepcopy(entry))
            return copied

    def add(self, entry):
        if not self._valid(entry):
            raise ValueError("Invalid intake entry")
        with self._lock:
            kept = []
            for existing in self._entries:
                if existing["id"] == entry["id"]:
                    for name in ("text", "source", "meta"):
                        if existing[name] != entry[name]:
                            raise ValueError("Turn identity belongs to another input")
                    return
                kept.append(existing)
            kept.append(deepcopy(entry))
            self._write(kept)
            self._entries = kept

    def remove(self, turn_id):
        with self._lock:
            kept = []
            found = False
            for existing in self._entries:
                if existing["id"] == turn_id:
                    found = True
                    continue
                kept.append(existing)
            if not found:
                return
            self._write(kept)
            self._entries = kept

    def _write(self, entries):
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        payload = {"version": JOURNAL_VERSION, "entries": entries}
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        sync_directory(self.root)
