"""Observed file-change receipts and streamed-response identity tests."""

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.aethermind import AetherMindClient
from goblin_core.aethermind_direct import DirectMcpClient
from goblin_core.capture import Capture
from goblin_core.config import default_settings, write_settings
from goblin_core.host import GoblinHost
from goblin_core.log import ChatLog
from goblin_core.session import GoblinSession
from goblin_core.tools import ApprovalRefused, build_registry


class FakeBackend:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = []

    def chat(self, messages, tools=None, **options):
        self.messages.append(messages)
        if self.responses:
            return self.responses.pop(0)
        return {"content": "done", "tool_calls": [], "usage": {}, "finish_reason": "stop"}

    def cancel(self):
        pass


class StreamingFailureBackend:
    def __init__(self, text):
        self.text = text
        self.on_delta = None

    def chat(self, messages, tools=None, **options):
        if self.on_delta is not None:
            self.on_delta("text", self.text)
        raise RuntimeError("stream broke")

    def cancel(self):
        pass


class StreamingWaitingBackend:
    def __init__(self, text):
        self.text = text
        self.on_delta = None
        self.entered = threading.Event()
        self.release = threading.Event()

    def chat(self, messages, tools=None, **options):
        if self.on_delta is not None:
            self.on_delta("text", self.text)
        self.entered.set()
        self.release.wait(5)
        return {"content": "late answer", "tool_calls": [], "usage": {},
                "finish_reason": "stop"}

    def cancel(self):
        self.release.set()


class StreamingSuccessBackend:
    def __init__(self, text):
        self.text = text
        self.on_delta = None

    def chat(self, messages, tools=None, **options):
        if self.on_delta is not None:
            self.on_delta("text", self.text)
        return {"content": self.text, "tool_calls": [], "usage": {},
                "finish_reason": "stop"}

    def cancel(self):
        pass


def make_registry(temp, approval='never', approve=None):
    workspace = Path(temp) / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    return build_registry(workspace, approval=approval, approve=approve), workspace


def make_session(temp, backend, delta_sink=None):
    home = Path(temp)
    values = default_settings(json.loads((ROOT / "product.json").read_text(encoding="utf-8")))
    values["workspace"] = str(home / "workspace")
    values["approval"] = "ask"
    config = write_settings(home, ROOT / "product.json", values)
    Path(config.workspace).mkdir(parents=True, exist_ok=True)
    client = AetherMindClient(direct_client=DirectMcpClient(), create_project=True)
    root = home / "chat" / config.slug
    capture = Capture(client, root / "outbox", config.aethermind.project, author="goblin")
    session = GoblinSession(lambda: config, lambda: backend, root, capture=capture,
                            approval=lambda name, args: True, delta_sink=delta_sink)
    return session, config


class RegistryDiffTests(unittest.TestCase):
    def test_write_file_reports_an_added_text_diff(self):
        with tempfile.TemporaryDirectory() as temp:
            registry, _workspace = make_registry(temp)
            output = registry.execute('write_file', {'path': 'notes/hello.txt',
                                                     'text': 'alpha\nbeta\n'})
            diffs = registry.take_file_changes()
            self.assertEqual(output, 'Wrote 11 bytes to notes/hello.txt')
            self.assertEqual(len(diffs), 1)
            entry = diffs[0]
            self.assertEqual(entry['path'], 'notes/hello.txt')
            self.assertEqual(entry['change'], 'added')
            self.assertIn('--- /dev/null', entry['patch'])
            self.assertIn('+++ notes/hello.txt', entry['patch'])
            self.assertIn('+alpha', entry['patch'])
            self.assertIn('+beta', entry['patch'])

    def test_edit_file_reports_a_modified_diff(self):
        with tempfile.TemporaryDirectory() as temp:
            registry, _workspace = make_registry(temp)
            registry.execute('write_file', {'path': 'notes.txt', 'text': 'alpha\nbeta\n'})
            registry.take_file_changes()
            registry.execute('edit_file', {'path': 'notes.txt', 'old': 'beta', 'new': 'BETA'})
            diffs = registry.take_file_changes()
            self.assertEqual(len(diffs), 1)
            entry = diffs[0]
            self.assertEqual(entry['path'], 'notes.txt')
            self.assertEqual(entry['change'], 'modified')
            self.assertIn('--- notes.txt', entry['patch'])
            self.assertIn('+++ notes.txt', entry['patch'])
            self.assertIn('-beta', entry['patch'])
            self.assertIn('+BETA', entry['patch'])

    def test_apply_patch_reports_each_file_operation(self):
        with tempfile.TemporaryDirectory() as temp:
            registry, workspace = make_registry(temp)
            (workspace / 'old.txt').write_text('keep\nremove\n', encoding='utf-8')
            (workspace / 'gone.txt').write_text('bye\n', encoding='utf-8')
            patch = '\n'.join([
                '*** Begin Patch',
                '*** Add File: new.txt',
                '+fresh',
                '+lines',
                '*** Update File: old.txt',
                '@@',
                ' keep',
                '-remove',
                '+stay',
                '*** Delete File: gone.txt',
                '*** End Patch',
            ])
            output = registry.execute('apply_patch', {'patch': patch})
            diffs = registry.take_file_changes()
            self.assertEqual(output, 'added new.txt; Updated old.txt; deleted gone.txt')
            self.assertEqual([entry['path'] for entry in diffs],
                             ['new.txt', 'old.txt', 'gone.txt'])
            self.assertEqual([entry['change'] for entry in diffs],
                             ['added', 'modified', 'deleted'])
            self.assertIn('+fresh', diffs[0]['patch'])
            self.assertIn('+lines', diffs[0]['patch'])
            self.assertIn('-remove', diffs[1]['patch'])
            self.assertIn('+stay', diffs[1]['patch'])
            self.assertIn('-bye', diffs[2]['patch'])
            self.assertFalse((workspace / 'gone.txt').exists())

    def test_failed_edit_reports_no_observed_change(self):
        with tempfile.TemporaryDirectory() as temp:
            registry, workspace = make_registry(temp)
            (workspace / 'dup.txt').write_text('same same\n', encoding='utf-8')
            with self.assertRaises(ValueError):
                registry.execute('edit_file', {'path': 'dup.txt', 'old': 'same',
                                               'new': 'different'})
            self.assertEqual(registry.take_file_changes(), [])
            self.assertEqual((workspace / 'dup.txt').read_text(encoding='utf-8'), 'same same\n')

    def test_failed_patch_keeps_the_real_earlier_change(self):
        with tempfile.TemporaryDirectory() as temp:
            registry, workspace = make_registry(temp)
            patch = '\n'.join([
                '*** Begin Patch',
                '*** Add File: first.txt',
                '+written before the failure',
                '*** Update File: missing.txt',
                '@@',
                '-gone',
                '+back',
                '*** End Patch',
            ])
            with self.assertRaises(Exception):
                registry.execute('apply_patch', {'patch': patch})
            diffs = registry.take_file_changes()
            self.assertEqual(len(diffs), 1)
            self.assertEqual(diffs[0]['path'], 'first.txt')
            self.assertEqual(diffs[0]['change'], 'added')
            self.assertTrue((workspace / 'first.txt').exists())

    def test_refused_write_reports_no_observed_change(self):
        with tempfile.TemporaryDirectory() as temp:
            registry, workspace = make_registry(temp, approval='ask',
                                                approve=lambda name, arguments: False)
            with self.assertRaises(ApprovalRefused):
                registry.execute('write_file', {'path': 'refused.txt', 'text': 'no'})
            self.assertEqual(registry.take_file_changes(), [])
            self.assertFalse((workspace / 'refused.txt').exists())

    def test_binary_copy_is_marked_binary(self):
        with tempfile.TemporaryDirectory() as temp:
            registry, workspace = make_registry(temp)
            (workspace / 'blob.bin').write_bytes(b'\x00\x01\xff\xfe')
            registry.execute('copy_path', {'src': 'blob.bin', 'dest': 'copy/blob.bin'})
            diffs = registry.take_file_changes()
            self.assertEqual(len(diffs), 1)
            entry = diffs[0]
            self.assertEqual(entry['path'], 'copy/blob.bin')
            self.assertEqual(entry['change'], 'binary')
            self.assertEqual(entry['patch'], '')
            self.assertIn('binary content', entry['detail'])
            self.assertEqual(entry['bytes_after'], 4)

    def test_delete_reports_a_deleted_text_patch(self):
        with tempfile.TemporaryDirectory() as temp:
            registry, _workspace = make_registry(temp)
            registry.execute('write_file', {'path': 'gone.txt', 'text': 'first\nsecond\n'})
            registry.take_file_changes()
            registry.execute('delete_path', {'path': 'gone.txt'})
            diffs = registry.take_file_changes()
            self.assertEqual(len(diffs), 1)
            entry = diffs[0]
            self.assertEqual(entry['path'], 'gone.txt')
            self.assertEqual(entry['change'], 'deleted')
            self.assertIn('--- gone.txt', entry['patch'])
            self.assertIn('+++ /dev/null', entry['patch'])
            self.assertIn('-first', entry['patch'])
            self.assertIn('-second', entry['patch'])

    def test_move_reports_both_sides_of_the_change(self):
        with tempfile.TemporaryDirectory() as temp:
            registry, _workspace = make_registry(temp)
            registry.execute('write_file', {'path': 'a.txt', 'text': 'moved\n'})
            registry.take_file_changes()
            registry.execute('move_path', {'src': 'a.txt', 'dest': 'sub/b.txt'})
            diffs = registry.take_file_changes()
            self.assertEqual(len(diffs), 2)
            self.assertEqual(diffs[0]['path'], 'a.txt')
            self.assertEqual(diffs[0]['change'], 'deleted')
            self.assertEqual(diffs[0]['moved_to'], 'sub/b.txt')
            self.assertEqual(diffs[1]['path'], 'sub/b.txt')
            self.assertEqual(diffs[1]['change'], 'added')
            self.assertEqual(diffs[1]['moved_from'], 'a.txt')
            self.assertIn('+moved', diffs[1]['patch'])

    def test_move_into_an_existing_directory_links_the_real_file(self):
        with tempfile.TemporaryDirectory() as temp:
            registry, workspace = make_registry(temp)
            registry.execute('write_file', {'path': 'a.txt', 'text': 'moved\n'})
            registry.take_file_changes()
            (workspace / 'archive').mkdir()
            registry.execute('move_path', {'src': 'a.txt', 'dest': 'archive'})
            diffs = registry.take_file_changes()
            self.assertEqual([entry['path'] for entry in diffs],
                             ['a.txt', 'archive/a.txt'])
            self.assertEqual(diffs[0]['moved_to'], 'archive/a.txt')
            self.assertEqual(diffs[1]['moved_from'], 'a.txt')
            self.assertEqual(diffs[1]['change'], 'added')

    def test_directory_copy_reports_each_added_file(self):
        with tempfile.TemporaryDirectory() as temp:
            registry, workspace = make_registry(temp)
            tree = workspace / 'tree'
            (tree / 'nested').mkdir(parents=True)
            (tree / 'a.txt').write_text('a\n', encoding='utf-8')
            (tree / 'nested' / 'b.txt').write_text('b\n', encoding='utf-8')
            registry.execute('copy_path', {'src': 'tree', 'dest': 'tree_copy'})
            diffs = registry.take_file_changes()
            self.assertEqual([entry['path'] for entry in diffs],
                             ['tree_copy/a.txt', 'tree_copy/nested/b.txt'])
            self.assertEqual([entry['change'] for entry in diffs], ['added', 'added'])
            self.assertIn('+a', diffs[0]['patch'])
            self.assertIn('+b', diffs[1]['patch'])

    def test_oversize_file_is_reported_as_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            registry, workspace = make_registry(temp)
            big = workspace / 'big.txt'
            big.write_bytes(b'x' * (2 * 1024 * 1024 + 10))
            registry.execute('delete_path', {'path': 'big.txt'})
            diffs = registry.take_file_changes()
            self.assertEqual(len(diffs), 1)
            entry = diffs[0]
            self.assertEqual(entry['path'], 'big.txt')
            self.assertEqual(entry['change'], 'unavailable')
            self.assertEqual(entry['patch'], '')
            self.assertIn('snapshot limit', entry['detail'])

    def test_observation_failure_does_not_block_the_write(self):
        with tempfile.TemporaryDirectory() as temp:
            registry, workspace = make_registry(temp)
            from goblin_core import tools as tools_module
            original = tools_module.observe_path
            calls = {'count': 0}

            def broken(path):
                calls['count'] += 1
                if calls['count'] == 1:
                    raise RuntimeError('snapshot failed')
                return original(path)

            tools_module.observe_path = broken
            try:
                output = registry.execute('write_file', {'path': 'safe.txt', 'text': 'ok\n'})
                diffs = registry.take_file_changes()
            finally:
                tools_module.observe_path = original
            self.assertEqual(output, 'Wrote 3 bytes to safe.txt')
            self.assertTrue((workspace / 'safe.txt').exists())
            self.assertEqual(diffs[0]['change'], 'unavailable')
            self.assertIn('observation failed', diffs[0]['detail'])

    def test_shell_reports_no_fabricated_change(self):
        with tempfile.TemporaryDirectory() as temp:
            registry, workspace = make_registry(temp)
            registry.execute('shell', {'command': 'printf shell > shell.txt'})
            self.assertEqual(registry.take_file_changes(), [])
            self.assertTrue((workspace / 'shell.txt').exists())


class SessionWireTests(unittest.TestCase):
    def test_failed_request_preserves_streamed_text_as_partial(self):
        with tempfile.TemporaryDirectory() as temp:
            events = []
            backend = StreamingFailureBackend("half an answer")
            session, config = make_session(
                temp, backend,
                delta_sink=lambda kind, text: events.append((kind, text)))
            try:
                with self.assertRaises(RuntimeError):
                    session.run_turn("go")
                messages = list(session.log.iter())
                self.assertEqual([message.kind for message in messages],
                                 ["user", "agent", "agent"])
                partial = messages[1]
                self.assertEqual(partial.text, "half an answer")
                self.assertTrue(partial.meta["partial"])
                self.assertTrue(partial.meta["failed"])
                self.assertFalse(partial.meta["success"])
                response_ids = [text for kind, text in events if kind == "model_start"]
                self.assertEqual(partial.meta["response_id"], response_ids[0])
                self.assertIn("Turn failed (RuntimeError)", messages[2].text)
                self.assertNotIn("response_id", messages[2].meta)

                class RowHost:
                    _config = config

                rows = GoblinHost._message_rows(RowHost(), partial, "turn-1", "chat")
                self.assertFalse(rows[0]["final"])
                self.assertEqual(rows[0]["response_id"], response_ids[0])
            finally:
                session.close()

    def test_interrupted_request_preserves_streamed_text_as_partial(self):
        with tempfile.TemporaryDirectory() as temp:
            events = []
            backend = StreamingWaitingBackend("partial answer")
            session, config = make_session(
                temp, backend,
                delta_sink=lambda kind, text: events.append((kind, text)))
            holder = {}
            thread = threading.Thread(target=lambda: holder.setdefault(
                "reply", session.run_turn("go")))
            try:
                thread.start()
                self.assertTrue(backend.entered.wait(3))
                self.assertTrue(session.interrupt())
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(holder["reply"], "Interrupted by the user.")
                messages = list(session.log.iter())
                self.assertEqual([message.kind for message in messages],
                                 ["user", "agent", "agent"])
                partial = messages[1]
                self.assertEqual(partial.text, "partial answer")
                self.assertTrue(partial.meta["partial"])
                self.assertTrue(partial.meta["interrupted"])
                self.assertFalse(partial.meta["success"])
                response_ids = [text for kind, text in events if kind == "model_start"]
                self.assertEqual(partial.meta["response_id"], response_ids[0])
                self.assertEqual(messages[2].text, "Interrupted by the user.")
                self.assertNotIn("response_id", messages[2].meta)

                class RowHost:
                    _config = config

                rows = GoblinHost._message_rows(RowHost(), partial, "turn-1", "chat")
                self.assertFalse(rows[0]["final"])
                notice = GoblinHost._message_rows(RowHost(), messages[2], "turn-1", "chat")
                self.assertTrue(notice[0]["final"])
            finally:
                session.close()

    def test_successful_stream_is_not_duplicated(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = StreamingSuccessBackend("one answer")
            session, _config = make_session(temp, backend)
            try:
                self.assertEqual(session.run_turn("go"), "one answer")
                messages = list(session.log.iter())
                self.assertEqual([message.kind for message in messages],
                                 ["user", "agent"])
                self.assertEqual(messages[1].text, "one answer")
                self.assertNotIn("partial", messages[1].meta)
            finally:
                session.close()

    def test_echo_diffs_and_response_id_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            delta_events = []
            backend = FakeBackend([
                {"content": "", "tool_calls": [
                    {"id": "c1", "name": "write_file",
                     "arguments": {"path": "notes/a.txt", "text": "A\n"}},
                    {"id": "c2", "name": "write_file",
                     "arguments": {"path": "notes/b.txt", "text": "B\n"}},
                ], "usage": {}, "finish_reason": "tool_calls"},
                {"content": "wrote both", "tool_calls": [], "usage": {}, "finish_reason": "stop"},
            ])
            session, config = make_session(
                temp, backend,
                delta_sink=lambda kind, text: delta_events.append((kind, text)))
            try:
                self.assertEqual(session.run_turn("write both"), "wrote both")
                echo_a = session.log.get(2)
                echo_b = session.log.get(4)
                self.assertEqual([entry['path'] for entry in echo_a.meta['diffs']],
                                 ['notes/a.txt'])
                self.assertEqual([entry['path'] for entry in echo_b.meta['diffs']],
                                 ['notes/b.txt'])
                self.assertIn('+A', echo_a.meta['diffs'][0]['patch'])
                self.assertIn('+B', echo_b.meta['diffs'][0]['patch'])
                response_ids = [text for kind, text in delta_events if kind == 'model_start']
                self.assertEqual(len(response_ids), 2)
                self.assertNotEqual(response_ids[0], response_ids[1])
                for response_id in response_ids:
                    self.assertEqual(len(response_id), 32)
                    int(response_id, 16)
                agent = session.log.get(5)
                self.assertEqual(agent.meta['response_id'], response_ids[1])
            finally:
                session.close()
            log = ChatLog(Path(temp) / "chat" / config.slug / "log")
            try:
                messages = list(log.iter())
            finally:
                log.close()
            echo_a = messages[2]
            echo_b = messages[4]
            self.assertEqual(echo_a.meta['diffs'][0]['path'], 'notes/a.txt')
            self.assertEqual(echo_b.meta['diffs'][0]['path'], 'notes/b.txt')
            self.assertTrue(echo_a.meta['diffs'][0]['patch'].startswith('--- /dev/null'))

            class RowHost:
                _config = config

            agent_rows = GoblinHost._message_rows(RowHost(), messages[5], "turn-1", "chat")
            agent_row = [row for row in agent_rows if row['kind'] == 'agent'][-1]
            self.assertEqual(agent_row['response_id'], messages[5].meta['response_id'])
            echo_rows = GoblinHost._message_rows(RowHost(), echo_a, "turn-1", "chat")
            self.assertEqual(echo_rows[0]['diffs'][0]['path'], 'notes/a.txt')

    def test_host_stream_turns_model_start_into_response_id(self):
        events = []

        class StreamHost:
            def _emit(self, event, data):
                events.append((event, data))

        GoblinHost._stream(StreamHost(), "model_start", "abc123")
        self.assertEqual(events, [("activity", {"phase": "model_start", "text": "",
                                                "response_id": "abc123"})])


if __name__ == "__main__":
    unittest.main()
