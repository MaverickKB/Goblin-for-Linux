"""A visible response attempt must never be replayed into the same stream."""

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Runtime'))

from goblin_core.backend import Backend, BackendError


class Response:
    headers = {'Content-Type': 'text/event-stream'}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def close(self):
        pass


class StreamAttempts:
    path = '/chat/completions'

    def __init__(self, kind):
        self.kind = kind
        self.attempts = 0

    def encode(self, model, messages, tools, options):
        return {'model': model, 'messages': messages}

    def headers(self, key, options):
        return {}

    def assemble(self, response, emit):
        self.attempts += 1
        if self.attempts == 1:
            if self.kind:
                emit(self.kind, 'first attempt')
            raise OSError('Disconnected stream')
        emit('text', 'second attempt')
        return {'content': 'second attempt', 'tool_calls': []}

    def decode(self, result):
        return result


class Opener:
    def open(self, request, timeout):
        return Response()


class StreamRetryTests(unittest.TestCase):
    def backend(self, kind):
        backend = Backend('http://fixture.invalid', 'fixture', 'chat_completions', retries=1)
        backend.adapter = StreamAttempts(kind)
        backend._opener = Opener()
        return backend

    def test_partial_text_thought_and_tool_output_prevent_replay(self):
        for kind in ('text', 'thought', 'tool'):
            with self.subTest(kind=kind):
                backend = self.backend(kind)
                output = []
                backend.on_delta = lambda kind, text: output.append((kind, text))
                with self.assertRaises(BackendError):
                    backend.chat([{'role': 'user', 'content': 'Continue'}])
                self.assertEqual(backend.adapter.attempts, 1)
                self.assertNotIn(('text', 'second attempt'), output)

    def test_transport_failure_before_output_can_retry(self):
        backend = self.backend(None)
        result = backend.chat([{'role': 'user', 'content': 'Continue'}])
        self.assertEqual(result['content'], 'second attempt')
        self.assertEqual(backend.adapter.attempts, 2)

    def test_status_updates_do_not_prevent_retry(self):
        backend = self.backend('status')
        result = backend.chat([{'role': 'user', 'content': 'Continue'}])
        self.assertEqual(result['content'], 'second attempt')

    def test_background_stream_preserves_the_same_attempt_rule(self):
        backend = self.backend('text')
        with self.assertRaises(BackendError):
            backend.chat([{'role': 'user', 'content': 'Summarize'}])
        self.assertEqual(backend.adapter.attempts, 1)

    def test_failed_stream_preserves_only_its_actual_partial_text(self):
        from test_session import make_session

        with tempfile.TemporaryDirectory() as temp:
            backend = self.backend('text')
            session, _ = make_session(temp, backend, delta_sink=lambda kind, text: None)
            try:
                with self.assertRaises(BackendError):
                    session.run_turn('Continue')
                partials = []
                for row in session.log.iter():
                    if row.meta.get('partial'):
                        partials.append(row.text)
                self.assertEqual(partials, ['first attempt'])
                self.assertEqual(backend.adapter.attempts, 1)
            finally:
                session.close()

    def test_stop_preserves_partial_attempt_without_retry(self):
        from test_session import make_session

        with tempfile.TemporaryDirectory() as temp:
            backend = self.backend('text')
            session, _ = make_session(temp, backend)

            def stop_after_text(kind, text):
                if kind == 'text':
                    session.interrupt()

            session.delta_sink = stop_after_text
            try:
                self.assertEqual(session.run_turn('Continue'), 'Interrupted by the user.')
                partials = []
                for row in session.log.iter():
                    if row.meta.get('partial'):
                        partials.append(row.text)
                self.assertEqual(partials, ['first attempt'])
                self.assertEqual(backend.adapter.attempts, 1)
            finally:
                session.close()
