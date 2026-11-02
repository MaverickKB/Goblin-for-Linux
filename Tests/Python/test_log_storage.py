"""Indexed durable records retain exact history with bounded decoded caching."""
from dataclasses import asdict
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Runtime'))

from goblin_core import log as log_module
from goblin_core.log import ChatLog, _object_bytes


class LogStorageTests(unittest.TestCase):
    def test_unicode_and_metadata_reopen_without_resident_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = ChatLog(root, cache_bytes=4096)
            expected = []
            for index in range(30):
                message = log.append('user', '日本語 🧌 café ' + str(index),
                                     {'nested': {'list': [index, 'é']}})
                expected.append(asdict(message))
            self.assertLessEqual(log._cache_bytes, 4096)
            log.close()
            with ChatLog(root, cache_bytes=4096) as reopened:
                self.assertEqual(reopened.count(), 30)
                self.assertEqual(reopened._cache_bytes, 0)
                self.assertFalse(reopened._cache)
                self.assertEqual([asdict(message) for message in reopened.iter()], expected)
                self.assertLessEqual(reopened._cache_bytes, 4096)
                self.assertEqual(len(reopened._record_offsets) * 20,
                                 len(reopened._record_offsets.tobytes())
                                 + len(reopened._record_lengths.tobytes())
                                 + len(reopened._record_files.tobytes()))

    def test_cache_evicts_least_recently_used_decoded_records(self):
        with tempfile.TemporaryDirectory() as temp:
            log = ChatLog(Path(temp), cache_bytes=0)
            try:
                for index in range(4):
                    log.append('user', 'Record ' + str(index))
                first = log.get(0)
                cost = _object_bytes(first, set())
                log._cache_limit_bytes = cost * 2 + 128
                log.get(0)
                log.get(1)
                log.get(0)
                log.get(2)
                self.assertIn(0, log._cache)
                self.assertNotIn(1, log._cache)
                self.assertIn(2, log._cache)
                self.assertLessEqual(log._cache_bytes, log._cache_limit_bytes)
                returned = log.get(0)
                returned.text = 'Changed by a caller'
                self.assertEqual(log.get(0).text, 'Record 0')
            finally:
                log.close()

    def test_oversized_record_remains_exact_without_entering_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            with ChatLog(Path(temp), cache_bytes=1024) as log:
                original = '🧌日本語' * 10000
                log.append('tool', original, {'payload': original})
                self.assertEqual(log.get(0).text, original)
                self.assertEqual(log._cache_bytes, 0)
                self.assertFalse(log._cache)

    def test_iteration_is_lazy_and_excludes_subsequent_appends(self):
        with tempfile.TemporaryDirectory() as temp:
            with ChatLog(Path(temp)) as log:
                for index in range(3):
                    log.append('user', str(index))
                with patch.object(log, 'get', wraps=log.get) as read:
                    records = log.iter(1)
                    read.assert_not_called()
                    log.append('agent', 'after snapshot')
                    self.assertEqual([message.text for message in records], ['1', '2'])
                    self.assertEqual(read.call_count, 2)
                self.assertEqual([message.text for message in log.iter(-1)], ['after snapshot'])

    def test_raw_record_byte_pages_reassemble_exact_disk_record(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with ChatLog(root, cache_bytes=0) as log:
                message = log.append('tool', 'é🧌日本語' * 100, {'nested': ['one', 2]})
                expected = next(root.glob('*.jsonl')).read_bytes()
                chunks = []
                for offset in range(0, log.record_size(0), 7):
                    chunks.append(log.record_bytes(0, offset, 7))
                self.assertEqual(b''.join(chunks), expected)
                self.assertEqual(json.loads(b''.join(chunks)), asdict(message))
                self.assertEqual(log.record_bytes(0, len(expected)), b'')
                self.assertEqual(log._cache_bytes, 0)
                with self.assertRaises(ValueError):
                    log.record_bytes(0, -1)
                with self.assertRaises(ValueError):
                    log.record_bytes(0, 0, 65537)
                with self.assertRaises(IndexError):
                    log.record_size(-1)

    def test_recovery_preserves_tail_then_indexes_new_append(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with ChatLog(root) as log:
                log.append('user', 'Committed')
            path = next(root.glob('*.jsonl'))
            committed = path.read_bytes()
            fragment = b'{"i": 1, "text": "partial'
            path.write_bytes(committed + fragment)
            with ChatLog(root) as reopened:
                self.assertEqual(reopened.count(), 1)
                appended = reopened.append('agent', 'Recovered')
                self.assertEqual(appended.i, 1)
                self.assertEqual(reopened.get(1).text, 'Recovered')
            self.assertEqual(next(root.glob('*.incomplete-*')).read_bytes(), fragment)
            with ChatLog(root) as reopened:
                self.assertEqual([message.text for message in reopened.iter()], ['Committed', 'Recovered'])

    def test_invalid_and_noncontiguous_committed_records_are_rejected(self):
        for replacement in [b'broken json\n', b'{"i": 4, "kind": "user", "text": "gap", "size": 3, "date": "today", "meta": {}}\n']:
            with self.subTest(record=replacement), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / '2026-01-01.jsonl').write_bytes(replacement)
                with self.assertRaises(ValueError):
                    ChatLog(root)

    def test_record_decode_does_not_block_count_or_index_reads(self):
        with tempfile.TemporaryDirectory() as temp:
            with ChatLog(Path(temp), cache_bytes=0) as log:
                log.append('user', 'A durable record to decode.')
                entered = threading.Event()
                release = threading.Event()
                counted = threading.Event()
                original = json.loads

                def decode(data, *args, **kwargs):
                    if threading.current_thread().name == 'blocked-history':
                        entered.set()
                        release.wait(3)
                    return original(data, *args, **kwargs)

                def count():
                    self.assertEqual(log.count(), 1)
                    self.assertGreater(log.record_size(0), 0)
                    counted.set()

                with patch.object(log_module.json, 'loads', side_effect=decode):
                    reader = threading.Thread(target=lambda: log.get(0), name='blocked-history')
                    reader.start()
                    counter = threading.Thread(target=count)
                    try:
                        self.assertTrue(entered.wait(1))
                        counter.start()
                        self.assertTrue(counted.wait(1))
                    finally:
                        release.set()
                        reader.join(3)
                        if counter.ident is not None:
                            counter.join(3)

    def test_simultaneous_cache_fills_count_one_retained_record(self):
        with tempfile.TemporaryDirectory() as temp:
            with ChatLog(Path(temp), cache_bytes=0) as log:
                log.append('user', 'Shared immutable record')
                log._cache_limit_bytes = 4096
                rendezvous = threading.Barrier(2)
                original = json.loads

                def decode(data, *args, **kwargs):
                    result = original(data, *args, **kwargs)
                    rendezvous.wait(3)
                    return result

                with patch.object(log_module.json, 'loads', side_effect=decode):
                    first = threading.Thread(target=lambda: log.get(0))
                    second = threading.Thread(target=lambda: log.get(0))
                    first.start()
                    second.start()
                    first.join(4)
                    second.join(4)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
                self.assertEqual(len(log._cache), 1)
                self.assertEqual(log._cache_bytes, log._cache[0][1])

    def test_exclusive_writer_lock_still_rejects_second_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            with ChatLog(Path(temp)):
                with self.assertRaises(RuntimeError):
                    ChatLog(Path(temp))


if __name__ == '__main__':
    unittest.main()
