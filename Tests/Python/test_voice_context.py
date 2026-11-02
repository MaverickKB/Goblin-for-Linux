"""Voice admission uses the same complete bounded conversation view."""

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.config import Config, default_settings, normalize_settings
from goblin_core.log import ChatLog
from goblin_core.tree import SummaryTree
from goblin_core.view import View
from goblin_core.voice import GoblinVoiceSessions, VoiceError


class VoiceContextTests(unittest.TestCase):
    def fixture(self, message):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        manifest = json.loads((ROOT / "product.json").read_text())
        values = normalize_settings(default_settings(manifest), manifest, root)
        config = Config(root, manifest, values)
        log = ChatLog(root / "log")
        self.addCleanup(log.close)
        log.append("user", message)
        tree = SummaryTree(root, log, node_bytes=512)
        view = View(root, tree, high_bytes=10000, low_bytes=5000)
        session = SimpleNamespace(log=log, view=view, system_prompt="Fixture instructions",
                                  capture=SimpleNamespace(inheritance_block=lambda: ""),
                                  tools=SimpleNamespace(schemas=lambda: []))
        host = SimpleNamespace(config=config, session_for=lambda slug: session)
        client = SimpleNamespace(mint=Mock(return_value={"value": "fixture-ephemeral",
                                                       "expires_at": 2000}),
                                 _secret="fixture-bearer")
        voice = GoblinVoiceSessions(host, client=client, clock=lambda: 1000)
        return voice, client, view

    def test_pending_gist_cannot_mint_or_open_a_voice_lease(self):
        voice, client, view = self.fixture("Long source. " * 200)
        self.assertIsNone(view.ready_snapshot())
        with self.assertRaises(VoiceError) as caught:
            voice.start("goblin")
        self.assertEqual(caught.exception.category, "context_pending")
        client.mint.assert_not_called()
        self.assertEqual(voice._leases, {})

    def test_ready_gist_opens_the_gate_and_keeps_the_full_context(self):
        voice, client, view = self.fixture("Keep the whole conversation.")
        context = view.ready_snapshot()
        self.assertIsNotNone(context)
        lease = voice.start("goblin")
        instructions = lease["session_update"]["session"]["instructions"]
        self.assertIn(context["gist"], instructions)
        self.assertEqual(lease["audio_sample_rate"], 24000)
        client.mint.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
