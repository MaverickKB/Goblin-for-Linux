"""Summary output records cost and never accepts a declared partial reply."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.backend import BackendError
from goblin_core.compactor import Compactor


class ReplyModel:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []

    def chat(self, messages, **options):
        self.requests.append(messages)
        return next(self.replies)


class CompactorResultTests(unittest.TestCase):
    def compactor(self, replies):
        model = ReplyModel(replies)
        tree = SimpleNamespace(node_bytes=64)
        view = SimpleNamespace(low_bytes=128)
        return Compactor(tree, view, model, "System", [], workers=1)

    def test_complete_line_retains_reported_usage(self):
        usage = {"input_tokens": 7, "output_tokens": 5}
        compactor = self.compactor([{
            "content": "A complete summary.", "finish_reason": "stop", "usage": usage}])
        result = compactor._model_line([], "source", 1)
        self.assertEqual(result.text, "A complete summary.")
        self.assertFalse(result.hard_cut)
        self.assertEqual(result.calls[0]["usage"], usage)
        self.assertGreaterEqual(result.calls[0]["elapsed_seconds"], 0)

    def test_overflow_retains_all_calls_and_the_actual_cut_flag(self):
        compactor = self.compactor([
            {"content": "x" * 70, "finish_reason": "stop"},
            {"content": "y" * 68, "finish_reason": "stop"},
        ])
        result = compactor._model_line([], "source", 1)
        self.assertEqual(result.text, "y" * 64)
        self.assertTrue(result.hard_cut)
        self.assertEqual(len(result.calls), 2)
        self.assertEqual(result.calls[0]["output_bytes"], 70)
        self.assertEqual(result.calls[1]["output_bytes"], 68)
        self.assertEqual(len(compactor.backend.requests), 2)

    def test_declared_incomplete_or_tool_reply_is_rejected(self):
        replies = [
            {"content": "No completion signal"},
            {"content": "Partial", "finish_reason": "length"},
            {"content": "Refusal", "finish_reason": "content_filter"},
            {"content": "Not a summary", "tool_calls": [{"name": "example"}]},
        ]
        for reply in replies:
            with self.subTest(reply=reply):
                compactor = self.compactor([reply])
                with self.assertRaises(BackendError) as raised:
                    compactor._model_line([], "source", 0)
                self.assertEqual(raised.exception.kind, "incomplete_response")


if __name__ == "__main__":
    unittest.main()
