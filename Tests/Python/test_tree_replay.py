"""Startup replay of the summary tree stays cheap on slow filesystems."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from goblin_core.log import ChatLog
from goblin_core.tree import SummaryTree


class TreeReplayTests(unittest.TestCase):
    def test_startup_replay_writes_once_per_level(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = ChatLog(root / 'log')
            try:
                for index in range(300):
                    log.append('user', 'Saved message ' + str(index))
                with patch('os.fsync') as fsync:
                    tree = SummaryTree(root, log, node_bytes=512)
                # One append for the level 0 file, so one fsync. Before the
                # batch replay this was one fsync per message.
                self.assertEqual(fsync.call_count, 1)
                for index in range(300):
                    self.assertEqual(tree.node(0, index), 'Saved message ' + str(index))
            finally:
                log.close()

    def test_reopening_a_complete_tree_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = ChatLog(root / 'log')
            try:
                for index in range(50):
                    log.append('user', 'Saved message ' + str(index))
                SummaryTree(root, log, node_bytes=512)
            finally:
                log.close()
            reopened = ChatLog(root / 'log')
            try:
                with patch('os.fsync') as fsync:
                    tree = SummaryTree(root, reopened, node_bytes=512)
                self.assertEqual(fsync.call_count, 0)
                self.assertEqual(tree.node(0, 49), 'Saved message 49')
                reopened.append('user', 'A later message')
                self.assertEqual(tree.node(0, 50), 'A later message')
            finally:
                reopened.close()


if __name__ == '__main__':
    unittest.main()
