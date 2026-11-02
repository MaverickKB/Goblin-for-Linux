"""Governing procedures reach real requests and remain stable within a turn."""
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from test_session import FakeBackend, make_session
from goblin_core.baseline import instruction_baseline, SUMMARY_ROLE
from goblin_core.skills import Shelf


def write_skill(root, name, body):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'SKILL.md').write_text('---\nname: ' + name + '\ndescription: Complete description for ' + name + '\n---\n' + body)


class InstructionBaselineTests(unittest.TestCase):
    def test_portable_procedures_are_retrievable_from_the_real_request_catalog(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FakeBackend([])
            session, _ = make_session(temp, backend)
            try:
                session.run_turn('Inspect the available working procedures.')
                prefix = backend.messages[0][0]['content']
                for name in ('pstack-poteto-mode', 'pstack-no-comments', 'pstack-reflect'):
                    self.assertIn(name + ' (builtin):', prefix)

                core = session.shelf.find('pstack-poteto-mode')['body']
                self.assertIn(core, prefix)
                self.assertIn('skill_read pstack-poteto-mode playbooks/<title>.md', core)
                playbook = session.tools.execute('skill_read', {
                    'name': 'pstack-poteto-mode', 'path': 'playbooks/bug-fix.md'})
                self.assertIn('Bug fix', playbook)

                procedure = session.tools.execute('skill_load', {'name': 'pstack-no-comments'})
                self.assertIn('references/comment-auditor.md', procedure)
                auditor = session.tools.execute('skill_read', {
                    'name': 'pstack-no-comments', 'path': 'references/comment-auditor.md'})
                for rule in ('Legal or license headers', 'public API contract', 'MUST KILL', 'REVIEW'):
                    self.assertIn(rule, auditor)

                reflect = session.tools.execute('skill_load', {'name': 'pstack-reflect'})
                self.assertNotIn('history_recall', reflect)
                for name in ('history_search', 'history_read', 'history_zoom'):
                    self.assertIn(name, reflect)
                    self.assertIn(name, session.tools.names())
                for lens in ('tooling', 'judgment', 'divergent'):
                    reviewer = session.tools.execute('skill_read', {
                        'name': 'pstack-reflect', 'path': 'references/' + lens + '-reviewer.md'})
                    self.assertIn('`skill_load`', reviewer)
                    self.assertIn('`skill_read`', reviewer)
                    self.assertIn('record indices', reviewer)
            finally:
                session.close()

    def test_bundled_relative_markdown_assets_are_present(self):
        with tempfile.TemporaryDirectory() as temp:
            session, _ = make_session(temp, FakeBackend([]))
            try:
                for path in session.shelf.bundled_root.rglob('*.md'):
                    for link in re.findall(r'\]\(([^)]+)\)', path.read_text()):
                        if '://' in link or link.startswith('#') or link == 'url':
                            continue
                        target = link.split('#')[0]
                        self.assertTrue((path.parent / target).exists(), str(path) + ': ' + link)
            finally:
                session.close()

    def test_real_foreground_requests_refresh_next_turn_and_after_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FakeBackend([
                {'content': '', 'tool_calls': [{'id': 'edit', 'name': 'edit_skill', 'arguments': {}}]},
                {'content': 'first complete', 'tool_calls': []},
                {'content': 'second complete', 'tool_calls': []},
            ])
            session, config = make_session(temp, backend)
            config.skills.note_bytes = 1
            user_root = session.shelf.user_root
            write_skill(user_root, 'pstack-unslop', 'ORIGINAL USER CORE BODY')
            write_skill(user_root, 'custom-last', 'Custom complete body')

            def edit_skill():
                write_skill(user_root, 'pstack-unslop', 'UPDATED USER CORE BODY')
                session.apply_config(config)
                return 'Updated core on disk.'

            session.tools.register('edit_skill', 'Edit a core override.', {'type': 'object'}, edit_skill)
            try:
                session.run_turn('Perform the edit and finish.')
                first = backend.messages[0][0]['content']
                self.assertEqual(first, backend.messages[1][0]['content'])
                self.assertIn('ORIGINAL USER CORE BODY', first)
                self.assertIn('already loaded and active', first)
                self.assertIn('Do not reload these same core bodies', first)
                self.assertNotIn('UPDATED USER CORE BODY', first)
                self.assertIn('custom-last (user): Complete description for custom-last', first)
                for skill in session.shelf.skills():
                    self.assertIn(skill['name'] + ' (' + skill['source'] + '): ' + ' '.join(skill['description'].split()), first)
                    if skill['name'].startswith('pstack-principle-'):
                        self.assertIn(skill['body'], first)
                session.run_turn('Continue with current instructions.')
                self.assertIn('UPDATED USER CORE BODY', backend.messages[-1][0]['content'])
            finally:
                session.close()
            resumed_backend = FakeBackend([])
            resumed, _ = make_session(temp, resumed_backend)
            try:
                resumed.run_turn('Resume the conversation.')
                self.assertIn('UPDATED USER CORE BODY', resumed_backend.messages[0][0]['content'])
                self.assertIn('<effective-skill-catalog>', resumed_backend.messages[0][0]['content'])
            finally:
                resumed.close()

    def test_optional_controls_keep_core_and_do_not_resurrect_retrieval(self):
        with tempfile.TemporaryDirectory() as temp:
            session, config = make_session(temp, FakeBackend([]))
            try:
                write_skill(session.shelf.user_root, 'pstack-unslop', 'DISABLED OVERRIDE')
                config.skills.disabled = ('pstack-unslop',)
                config.skills.enabled = False
                baseline = instruction_baseline(session.shelf)
                self.assertNotIn('DISABLED OVERRIDE', baseline)
                self.assertIn('pstack-poteto-mode', baseline)
                self.assertIn('pstack-unslop', baseline)
                self.assertNotIn('(builtin):', baseline)
                with self.assertRaises(ValueError):
                    session.shelf.load('pstack-unslop', 30000)
                config.skills.enabled = True
                session.shelf.allowed = ('custom-only',)
                baseline = instruction_baseline(session.shelf)
                self.assertIn('<procedure name="pstack-poteto-mode">', baseline)
                self.assertNotIn('DISABLED OVERRIDE', baseline)
            finally:
                session.close()

    def test_builtin_precedence_and_isolated_shelf_fixture(self):
        with tempfile.TemporaryDirectory() as temp:
            session, config = make_session(temp, FakeBackend([]))
            try:
                root = Path(temp)
                bundled = root / 'bundled'
                write_skill(bundled, 'example', 'BUILTIN')
                shelf = Shelf(root, root, config.skills, bundled_root=bundled)
                self.assertEqual(shelf.find('example')['body'], 'BUILTIN')
                write_skill(shelf.user_root, 'example', 'USER')
                self.assertEqual(shelf.find('example')['body'], 'USER')
                project = root / '.goblin' / 'skills'
                write_skill(project, 'example', 'PROJECT')
                config.skills.trusted_projects = (str(root),)
                config.skills.project_dirs = ('.goblin/skills',)
                self.assertEqual(shelf.find('example')['body'], 'PROJECT')
                assigned = root / 'assigned'
                write_skill(assigned, 'example', 'SESSION')
                shelf.session_roots = (assigned,)
                self.assertEqual(shelf.find('example')['body'], 'SESSION')
                isolated = Shelf(root, root, config.skills, bundled_root=None)
                self.assertFalse(any(skill['source'] == 'builtin' for skill in isolated.skills()))
            finally:
                session.close()

    def test_missing_declared_principle_is_rejected_even_with_user_override(self):
        with tempfile.TemporaryDirectory() as temp:
            session, config = make_session(temp, FakeBackend([]))
            try:
                source = session.shelf.bundled_root
                bundled = Path(temp) / 'baseline-copy'
                bundled.mkdir()
                shutil.copy(source / 'SOURCE.json', bundled / 'SOURCE.json')
                manifest = json.loads((bundled / 'SOURCE.json').read_text())
                for name in manifest['baseline_core']:
                    directory = bundled / name
                    directory.mkdir()
                    shutil.copy(source / name / 'SKILL.md', directory / 'SKILL.md')
                missing = 'pstack-principle-fix-root-causes'
                (bundled / missing / 'SKILL.md').unlink()
                write_skill(session.shelf.user_root, missing, 'An override cannot repair a broken distribution.')
                shelf = Shelf(Path(temp), Path(temp), config.skills, bundled_root=bundled)
                with self.assertRaisesRegex(RuntimeError, 'Missing or invalid bundled governing baseline skill: ' + missing):
                    instruction_baseline(shelf)
            finally:
                session.close()

    def test_worker_and_summary_requests_include_governing_baseline(self):
        with tempfile.TemporaryDirectory() as temp:
            backend = FakeBackend([])
            session, _ = make_session(temp, backend)
            try:
                started = session.agents.spawn('helper', 'Do the scoped work.')
                session.agents.wait(5)
                worker = session.agents._threads[started['id']]['session']
                self.assertIn('<governing-baseline>', backend.messages[0][0]['content'])
                self.assertIn('pstack-poteto-mode', worker.system_prompt)
                self.assertIn('pstack-unslop', worker.system_prompt)
                session.compactor.stop()
                row = session.log.append('user', 'An exact long source. ' * 100)
                session.compactor._stop.clear()
                session.compactor.backend = backend
                session.compactor._summarize(0, row.i)
                system = backend.messages[-1][0]['content']
                self.assertTrue(system.startswith(session.system_prompt))
                self.assertTrue(system.endswith(SUMMARY_ROLE))
                self.assertIn('Do not select or execute', system)
                self.assertIn('Summarize only the designated source.', system)
                self.assertIn('Do not repeat the baseline or catalog in the summary.', system)
            finally:
                session.close()


if __name__ == '__main__':
    unittest.main()
