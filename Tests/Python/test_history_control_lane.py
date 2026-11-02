"""Blocked immutable history work leaves snapshot and Stop admission available."""
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Runtime'))

import goblin_bridge
from goblin_core import log as log_module
from goblin_core.log import ChatLog
from test_release_bridge import RecordingStdout, run_reader, wait_until


class HistoryControlLaneTests(unittest.TestCase):
    def test_actual_record_decode_leaves_count_and_stop_available(self):
        with tempfile.TemporaryDirectory() as temp:
            with ChatLog(Path(temp), cache_bytes=0) as log:
                log.append('user', 'Saved history')
                entered = threading.Event()
                release = threading.Event()
                stopped = threading.Event()
                original = json.loads

                def decode(data, *args, **kwargs):
                    if isinstance(data, bytes):
                        entered.set()
                        release.wait(3)
                    return original(data, *args, **kwargs)

                def snapshot():
                    entered.wait(2)
                    return {'history_count': log.count()}

                def interrupt(_slug):
                    stopped.set()
                    return {'stopped': True}

                host = SimpleNamespace(
                    slug='fixture', session=SimpleNamespace(log=log), snapshot=snapshot,
                    history_page=lambda before, limit: {'text': log.get(0).text},
                    interrupt=interrupt, begin_shutdown=lambda: None, shutdown=lambda: None)
                with patch.object(goblin_bridge, 'GoblinHost', return_value=host):
                    bridge = goblin_bridge.Bridge(Path(temp), ROOT / 'product.json')
                output = RecordingStdout()
                requests = [
                    {'id': 'history', 'method': 'history/page'},
                    {'id': 'snapshot', 'method': 'snapshot'},
                    {'id': 'stop', 'method': 'chat/stop'},
                ]
                runner, start, old_stdin, old_stdout = run_reader(bridge, requests, output)
                try:
                    with patch.object(log_module.json, 'loads', side_effect=decode):
                        start()
                        self.assertTrue(entered.wait(1))
                        self.assertTrue(stopped.wait(1))
                        self.assertTrue(wait_until(lambda: any(
                            frame.get('id') == 'snapshot' for frame in output.frames()), 1))
                        self.assertFalse(release.is_set())
                        self.assertFalse(any(frame.get('id') == 'history' for frame in output.frames()))
                        release.set()
                        runner.join(4)
                    self.assertFalse(runner.is_alive())
                finally:
                    release.set()
                    runner.join(4)
                    sys.stdin = old_stdin
                    sys.stdout = old_stdout

    def test_blocked_snapshot_or_bootstrap_never_occupies_control_reader(self):
        for method in ('bootstrap', 'snapshot'):
            with self.subTest(method=method):
                entered = threading.Event()
                release = threading.Event()
                stopped = threading.Event()

                def snapshot():
                    entered.set()
                    release.wait(3)
                    return {}

                def interrupt(_slug):
                    stopped.set()
                    return {'stopped': True}

                host = SimpleNamespace(
                    slug='fixture', snapshot=snapshot, interrupt=interrupt,
                    begin_shutdown=lambda: None, shutdown=lambda: None)
                with patch.object(goblin_bridge, 'GoblinHost', return_value=host):
                    bridge = goblin_bridge.Bridge(Path('fixture'), ROOT / 'product.json')
                output = RecordingStdout()
                requests = [{'id': 'state', 'method': method}, {'id': 'stop', 'method': 'chat/stop'}]
                runner, start, old_stdin, old_stdout = run_reader(bridge, requests, output)
                try:
                    start()
                    self.assertTrue(entered.wait(1))
                    self.assertTrue(stopped.wait(1))
                    self.assertFalse(release.is_set())
                finally:
                    release.set()
                    runner.join(4)
                    sys.stdin = old_stdin
                    sys.stdout = old_stdout


if __name__ == '__main__':
    unittest.main()
