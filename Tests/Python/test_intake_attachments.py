"""Accepted attachment references survive the raw-log to native snapshot boundary."""

from pathlib import Path
import tempfile
import unittest

from Tests.Python.test_release_intake import make_host, release_settings
from goblin_core import attachments


class IdleWorker:
    def is_alive(self):
        return True


class IntakeAttachmentTests(unittest.TestCase):
    def test_cancelled_attachment_path_reaches_the_authoritative_message_row(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in ("direct", "alias"):
                with self.subTest(path_kind=name):
                    target = root / (name + "-home")
                    target.mkdir()
                    home = target
                    if name == "alias":
                        home = root / "linked-home"
                        home.symlink_to(target, target_is_directory=True)
                    host = make_host(home)
                    try:
                        host.save_settings(release_settings(home / "workspace"))
                        file = home / "note.txt"
                        file.write_text("attached content", encoding="utf-8")
                        references = host._ingest_attachments([str(file)])
                        expected_path = str(file.resolve())
                        self.assertEqual(references[0]["path"], expected_path)
                        host._worker = IdleWorker()
                        host.submit(host.slug, "Keep the attachment", client_request_id="attachment-a",
                                    attachments=references)
                        host.interrupt()
                        rows = []
                        for row in host.history_page()["messages"]:
                            if row["kind"] == "user" and row["turn_id"] == "attachment-a":
                                rows.append(row)
                        self.assertEqual(len(rows), 1)
                        self.assertEqual(rows[0]["status"], "cancelled")
                        self.assertEqual(rows[0]["attachments"], [expected_path])
                    finally:
                        host.shutdown()

    def test_an_image_attachment_reaches_the_model_as_a_data_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            session_dir = root / "session"
            session_dir.mkdir()
            png = bytes.fromhex(
                "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                "1f15c4890000000d4944415478da63f8cfc0f01f00050001ff89993d1d"
                "0000000049454e44ae426082")
            ref = attachments.store(session_dir, png, "screenshot.png")
            notes, images = attachments.describe(session_dir, [ref])
            self.assertEqual(len(images), 1)
            self.assertTrue(images[0]["data_url"].startswith("data:image/png;base64,"))
            self.assertIn("shown with this message", notes[0])


if __name__ == "__main__":
    unittest.main()
