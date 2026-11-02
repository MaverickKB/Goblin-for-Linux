"""Missing view.json rebuilds the gist from the durable chat log."""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.log import ChatLog
from goblin_core.tree import SummaryTree
from goblin_core.view import View


class ViewRecoveryTests(unittest.TestCase):
    def test_missing_view_rebuilds_every_record_from_the_log(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            records = [("user", "first question"), ("agent", "first answer"),
                       ("user", "second question"), ("agent", "second answer")]
            log = ChatLog(root / "log")
            for kind, text in records:
                log.append(kind, text)
            tree = SummaryTree(root, log, node_bytes=512)
            view = View(root, tree, high_bytes=10000, low_bytes=5000)
            view.save()
            (root / "view.json").unlink()
            log.close()

            again = ChatLog(root / "log")
            again_tree = SummaryTree(root, again, node_bytes=512)
            rebuilt = View(root, again_tree, high_bytes=10000, low_bytes=5000)

            self.assertTrue((root / "view.json").exists())
            self.assertEqual(rebuilt.nodes, [(0, 0), (0, 1), (0, 2), (0, 3)])
            for index, (_kind, text) in enumerate(records):
                self.assertEqual(rebuilt.zoom(index, 1), text)
                self.assertIn(text, rebuilt.render())
            again.close()

    def test_missing_tree_and_view_rebuild_from_the_raw_log(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = ChatLog(root / "log")
            log.append("user", "first question")
            log.append("agent", "first answer")
            tree = SummaryTree(root, log, node_bytes=512)
            view = View(root, tree, high_bytes=10000, low_bytes=5000)
            view.save()
            (root / "view.json").unlink()
            for path in (root / "tree").iterdir():
                path.unlink()
            log.close()

            again = ChatLog(root / "log")
            again_tree = SummaryTree(root, again, node_bytes=512)
            rebuilt = View(root, again_tree, high_bytes=10000, low_bytes=5000)

            self.assertEqual(again_tree.node(0, 0), "first question")
            self.assertEqual(again_tree.node(0, 1), "first answer")
            self.assertEqual(rebuilt.nodes, [(0, 0), (0, 1)])
            self.assertIn("first question", rebuilt.render())
            self.assertIn("first answer", rebuilt.render())
            again.close()

    def test_rebuild_reuses_built_summaries_and_keeps_compaction_running(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = ChatLog(root / "log")
            log.append("user", "first question")
            log.append("agent", "first answer")
            log.append("user", "second question")
            log.append("agent", "second answer")
            tree = SummaryTree(root, log, node_bytes=512)
            tree.set_node(1, 0, "first pair summary")
            view = View(root, tree, high_bytes=10000, low_bytes=5000)
            view.save()
            (root / "view.json").unlink()
            log.close()

            again = ChatLog(root / "log")
            again_tree = SummaryTree(root, again, node_bytes=512)
            rebuilt = View(root, again_tree, high_bytes=120, low_bytes=80)

            self.assertEqual(rebuilt.nodes, [(1, 0), (0, 2), (0, 3)])
            covered = 0
            for level, _index in rebuilt.nodes:
                covered += 1 << level
            self.assertEqual(covered, again.count())
            self.assertTrue(rebuilt.needs_compaction)
            self.assertEqual(again.get(3).text, "second answer")

            again_tree.set_node(1, 1, "second pair summary")
            again_tree.set_node(2, 0, "all four summary")

            self.assertEqual(rebuilt.nodes, [(2, 0)])
            self.assertFalse(rebuilt.needs_compaction)
            again.close()


if __name__ == "__main__":
    unittest.main()
