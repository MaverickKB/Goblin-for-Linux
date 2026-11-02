"""Expert storage edits use the same idle owner transition as setup selection."""
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from test_release_host import make_host
from goblin_core.floor_definition import default_definition, export_text, definition_path


class AutonomyBindingSaveTests(unittest.TestCase):
    def test_running_job_prevents_storage_edits_before_config_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(Path(temp))
            release = Path(temp) / 'finish-job'
            process = None
            try:
                host.save_settings(host.config.settings_dict())
                # Enable this temporary owner directly without starting a scheduler.
                host.autonomy.settings.enabled = True
                script = ('from pathlib import Path\nimport sys\nimport time\n'
                          'while not Path(sys.argv[1]).exists():\n    time.sleep(0.01)\n'
                          'print("Completed normally.")\n')
                command = shlex.join([sys.executable, '-c', script, str(release)])
                task = host.autonomy.start_task(host.slug, 'Owned test job', command)
                process = host.autonomy._children[task['id']]
                self.assertIsNone(process.poll())
                owner = host.autonomy
                config_path = Path(temp) / 'config.json'
                before = config_path.read_bytes()
                for field, value in (('state_file', 'other/state.json'), ('tasks_dir', 'other/tasks'),
                                     ('lesson_file', 'other/lessons.json'), ('floor_file', 'other/floor.json')):
                    with self.subTest(field=field):
                        values = host.config.settings_dict()
                        values['advanced'].setdefault('autonomy', {})[field] = value
                        with self.assertRaisesRegex(RuntimeError, 'Finish autonomy jobs'):
                            host.save_settings(values)
                        self.assertEqual(config_path.read_bytes(), before)
                        self.assertIs(host.autonomy, owner)
                        self.assertIsNone(process.poll())
                release.touch()
                process.wait(3)
                owner.active_actions()
                values = host.config.settings_dict()
                values['advanced'].setdefault('autonomy', {})['state_file'] = 'other/state.json'
                result = host.save_settings(values)
                self.assertEqual(result['settings_issue'], '')
                self.assertIsNot(host.autonomy, owner)
                self.assertEqual(host.autonomy.path, (host.home / 'other/state.json'))
                self.assertFalse(host.autonomy._production_paused)
            finally:
                release.touch()
                if process is not None:
                    process.wait(3)
                host.shutdown()

    def test_stale_floor_editor_cannot_write_after_settings_revision_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(Path(temp))
            try:
                old_revision = host.snapshot()['settings_revision']
                values = host.config.settings_dict()
                values['agent_name'] = 'Updated profile'
                host.save_settings(values)
                target = definition_path(host.config)
                self.assertFalse(target.exists())
                text = export_text(default_definition())
                with self.assertRaisesRegex(RuntimeError, 'floor editor was loaded'):
                    host.floor_save(text, expected_revision=old_revision)
                self.assertFalse(target.exists())
                host.floor_save(text, expected_revision=host.snapshot()['settings_revision'])
                self.assertTrue(target.exists())
                host.floor_save(text)
            finally:
                host.shutdown()


if __name__ == '__main__':
    unittest.main()
