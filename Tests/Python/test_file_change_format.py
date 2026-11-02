import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))
from goblin_core.file_changes import diff_observations, observe_path


class FileChangeFormatTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("patch"), "patch executable required for application proof")
    def test_patches_reproduce_line_endings_and_final_newline(self):
        cases = [("alpha", "alpha\n"), ("alpha\n", "alpha"),
                 ("one\r\ntwo\r\n", "one\r\nTHREE\r\n"),
                 ("one\u2028two", "one\u2028three")]
        for before, after in cases:
            with self.subTest(before=before, after=after), tempfile.TemporaryDirectory() as temp:
                target = Path(temp) / "sample.txt"
                target.write_bytes(before.encode("utf-8"))
                first = observe_path(target)
                target.write_bytes(after.encode("utf-8"))
                second = observe_path(target)
                rows = diff_observations("sample.txt", first, second)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["change"], "modified")
                target.write_bytes(before.encode("utf-8"))
                result = subprocess.run(["patch", "-p0"], input=rows[0]["patch"].encode("utf-8"),
                                        cwd=temp, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(target.read_bytes(), after.encode("utf-8"))

    def test_partial_tree_observation_does_not_invent_additions_or_deletions(self):
        with tempfile.TemporaryDirectory() as temp, patch("goblin_core.file_changes.MAX_TREE_FILES", 1):
            root = Path(temp)
            (root / "a.txt").write_text("a")
            (root / "b.txt").write_text("b")
            before = observe_path(root)
            (root / "0.txt").write_text("new")
            after = observe_path(root)
            rows = diff_observations("folder", before, after)
            self.assertTrue(rows)
            for row in rows:
                self.assertEqual(row["change"], "unavailable")
                self.assertEqual(row["patch"], "")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO supported on POSIX")
    def test_non_regular_files_are_reported_without_opening_them(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "pipe"
            os.mkfifo(target)
            result = observe_path(target)
            self.assertTrue(result.file.exists)
            self.assertEqual(result.file.issue, "not a regular file")


if __name__ == "__main__":
    unittest.main()
