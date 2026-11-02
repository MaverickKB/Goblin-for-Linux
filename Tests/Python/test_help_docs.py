import json
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'Runtime'))
from goblin_core.help_docs import HelpDocuments


class HelpDocumentsTests(unittest.TestCase):
    def test_catalog_read_pages_and_search_use_same_manual(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'index.json').write_text(json.dumps({'documents': [{'id': 'dev', 'title': 'Development', 'audience': 'developer', 'file': 'dev.md'}]}))
            (root / 'dev.md').write_text('First\nGist context\nThird\n')
            docs = HelpDocuments(root)
            self.assertEqual(docs.call()['documents'][0]['id'], 'dev')
            page = docs.call('read', 'dev', start_line=2, line_count=1)
            self.assertEqual(page['text'], 'Gist context')
            self.assertEqual(page['next_line'], 3)
            self.assertEqual(docs.call('search', query='GIST')['matches'][0]['line'], 2)
            with self.assertRaises(ValueError): docs.call('read', '../dev.md')
            with self.assertRaises(ValueError): docs.call('read', 'dev', start_line=0)

    def test_catalog_cannot_escape_via_path_or_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'help'
            root.mkdir()
            outside = Path(folder) / 'private.md'
            outside.write_text('private')
            index = root / 'index.json'
            index.write_text(json.dumps({'documents': [{'id': 'bad', 'file': '../private.md'}]}))
            with self.assertRaises(ValueError): HelpDocuments(root).call()
            index.write_text(json.dumps({'documents': [{'id': 'bad', 'file': 'link.md'}]}))
            (root / 'link.md').symlink_to(outside)
            with self.assertRaises(ValueError): HelpDocuments(root).call('read', 'bad')

    def test_session_exposes_help_without_bypassing_tool_ceiling(self):
        from test_session import FakeBackend, make_session
        with tempfile.TemporaryDirectory() as folder:
            session, _ = make_session(folder, FakeBackend([]))
            try:
                self.assertIn('app_help', session.tools.names())
                limited = session.tools.restricted(['read_file'])
                self.assertNotIn('app_help', limited.names())
            finally:
                session.close()
