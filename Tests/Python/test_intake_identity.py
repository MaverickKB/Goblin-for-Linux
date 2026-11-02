"""Durable intake identity and damaged-file preservation contracts."""

import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))
from goblin_core.intake import IntakeJournal


class IntakeIdentityTests(unittest.TestCase):
    def test_repeated_identity_cannot_replace_accepted_input(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = IntakeJournal(directory)
            original = {"id": "request-a", "text": "first input", "source": "chat",
                        "meta": {"request_id": "request-a", "attachments": ["image.png"]}}
            journal.add(original)
            before = journal.path.read_bytes()
            journal.add(dict(original, created=200))
            self.assertEqual(journal.path.read_bytes(), before)
            for name, replacement in [("text", "second input"), ("source", "voice"),
                                      ("meta", {"request_id": "request-a", "attachments": []})]:
                changed = dict(original)
                changed[name] = replacement
                with self.assertRaises(ValueError):
                    journal.add(changed)
                self.assertEqual(journal.path.read_bytes(), before)
            original["meta"]["attachments"].append("caller-change.png")
            returned = journal.entries()
            returned[0]["meta"]["attachments"].append("reader-change.png")
            self.assertEqual(journal.entries()[0]["meta"]["attachments"], ["image.png"])

    def test_damaged_entries_preserve_bytes_and_keep_valid_pending_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "intake"
            root.mkdir()
            path = root / "pending.json"
            valid = {"id": "accepted", "text": "keep this", "source": "chat", "meta": {}}
            damaged = {"id": "damaged", "text": "retain the original bytes"}
            payload = json.dumps({"version": 1, "entries": [valid, damaged]}).encode()
            path.write_bytes(payload)
            journal = IntakeJournal(directory)
            self.assertEqual(journal.entries(), [valid])
            preserved = list(root.glob("pending.json.corrupt-*"))
            self.assertEqual(len(preserved), 1)
            self.assertEqual(preserved[0].read_bytes(), payload)
            self.assertIn(str(preserved[0]), journal.recovery_notice)
            self.assertEqual(IntakeJournal(directory).entries(), [valid])

    def test_failed_preservation_cannot_overwrite_the_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "intake" / "pending.json"
            path.parent.mkdir()
            payload = b"unfinished journal bytes"
            path.write_bytes(payload)
            with mock.patch("goblin_core.intake.os.replace", side_effect=OSError("cannot preserve")):
                with self.assertRaises(OSError):
                    IntakeJournal(directory)
            self.assertEqual(path.read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
