"""Bounded presentation pages over the exact owned transcript."""
import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Runtime'))
from goblin_core.host import GoblinHost


class HistoryPageTests(unittest.TestCase):
    def test_runtime_snapshot_does_not_read_history_or_render_gist(self):
        with tempfile.TemporaryDirectory() as temp:
            host = GoblinHost(Path(temp), ROOT / 'product.json')
            try:
                host.session.compactor.stop()
                host.session.log.append('user', 'saved')
                with patch.object(host.session.log, 'iter', side_effect=AssertionError('archive scan')):
                    with patch.object(host.session.view, 'render', side_effect=AssertionError('gist render')):
                        # Compaction status itself computes view bytes; isolate that
                        # metric so this proves the runtime projection owns no history.
                        with patch.object(host.session.compactor, 'status', return_value={}):
                            state = host.snapshot()
                self.assertNotIn('messages', state)
                self.assertNotIn('gist', state)
                self.assertEqual(state['history_count'], 1)
            finally:
                host.shutdown()

    def test_every_record_reachable_and_oversized_unicode_details_are_exact(self):
        with tempfile.TemporaryDirectory() as temp:
            host = GoblinHost(Path(temp), ROOT / 'product.json')
            host.session.compactor.stop()
            try:
                for index in range(120):
                    host.session.log.append('user', 'Original ' + str(index), {'request_id': 'turn-' + str(index)})
                text = '漢字🐈 café\n' * 6000
                meta = {'attachments': [{'path': '/tmp/exact.txt'}],
                        'diffs': [{'path': 'exact.txt', 'patch': '+' + text}]}
                message = host.session.log.append('echo', text, meta)
                before = None
                seen = []
                while True:
                    page = host.history_page(before, 17)
                    self.assertLess(len(json.dumps(page, ensure_ascii=False).encode()), 65536)
                    seen[0:0] = [row['id'] for row in page['messages']]
                    if not page['has_more']:
                        break
                    self.assertNotEqual(page['before'], before)
                    before = page['before']
                self.assertEqual(seen, [str(index) for index in range(121)])
                newest = host.history_page()['messages'][-1]
                self.assertTrue(newest['truncated'])
                self.assertEqual(newest['record_i'], message.i)
                data = b''
                offset = 0
                while True:
                    chunk = host.history_record(message.i, offset)
                    self.assertLess(len(json.dumps(chunk).encode()), 24000)
                    data += base64.b64decode(chunk['data'])
                    offset = chunk['next_offset']
                    if offset == chunk['total']:
                        break
                restored = json.loads(data)
                self.assertEqual(restored['text'], text)
                self.assertEqual(restored['meta'], meta)
            finally:
                host.shutdown()

    def test_explicit_summary_read_retains_one_complete_view_across_chunks(self):
        with tempfile.TemporaryDirectory() as temp:
            host = GoblinHost(Path(temp), ROOT / 'product.json')
            host.session.compactor.stop()
            try:
                for index in range(300):
                    host.session.log.append('user', ('Summary ' + str(index) + ' ') * 10)
                expected = host.session.view.render()
                chunk = host.gist_page()
                self.assertLess(len(json.dumps(chunk).encode()), 24000)
                data = base64.b64decode(chunk['data'])
                self.assertLess(chunk['next_offset'], chunk['total'])
                host.session.log.append('user', 'Added after the summary read started')
                while chunk['next_offset'] < chunk['total']:
                    chunk = host.gist_page(chunk['token'], chunk['next_offset'])
                    self.assertLess(len(json.dumps(chunk).encode()), 24000)
                    data += base64.b64decode(chunk['data'])
                self.assertEqual(data.decode('utf-8'), expected)
                self.assertIsNone(host._gist_snapshot)
            finally:
                host.shutdown()

    def test_runtime_turn_projection_remains_bounded_with_a_large_queue(self):
        with tempfile.TemporaryDirectory() as temp:
            host = GoblinHost(Path(temp), ROOT / 'product.json')
            try:
                for index in range(400):
                    host._turns[str(index)] = {'id': str(index), 'state': 'queued',
                                               'text': 'Input ' * 10000, 'error': 'Error ' * 10000}
                host._active_turn = ('0', 'chat')
                turns = host.snapshot()['turns']
                self.assertLessEqual(len(turns), 64)
                self.assertIn('0', [row['id'] for row in turns])
                self.assertLess(len(json.dumps(turns).encode()), 150000)
                host._turns.clear()
            finally:
                host._turns.clear()
                host.shutdown()

    def test_oversized_identity_metadata_never_prevents_cursor_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            host = GoblinHost(Path(temp), ROOT / 'product.json')
            try:
                metadata = {'request_id': '巨大' * 30000, 'source': 'source' * 30000,
                            'response_id': 'response' * 30000}
                host.session.log.append('user', 'First', metadata)
                host.session.log.append('agent', 'Second', metadata)
                newest = host.history_page(limit=1)
                oldest = host.history_page(newest['before'], limit=1)
                self.assertEqual(newest['before'], 1)
                self.assertEqual(oldest['before'], 0)
                self.assertEqual(newest['messages'][0]['turn_id'], oldest['messages'][0]['turn_id'])
                self.assertLess(len(json.dumps(newest, ensure_ascii=False).encode()), 65536)
            finally:
                host.shutdown()

    def test_page_preserves_legacy_turn_identity_when_boundary_is_on_another_page(self):
        with tempfile.TemporaryDirectory() as temp:
            host = GoblinHost(Path(temp), ROOT / 'product.json')
            try:
                host.session.log.append('user', 'first')
                for index in range(6):
                    host.session.log.append('echo', 'result ' + str(index))
                page = host.history_page(limit=2)
                self.assertEqual([row['turn_id'] for row in page['messages']], ['history-0', 'history-0'])
            finally:
                host.shutdown()


if __name__ == '__main__':
    unittest.main()
