"""Autonomy definitions are shared; execution and retained files belong to profiles."""
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_release_host import make_host
from goblin_core.autonomy import Autonomy
from goblin_core.autonomy_setups import AutonomySetups
from goblin_core.floor_definition import audit_path, default_definition, load_definition, save_definition


class AutonomySetupTests(unittest.TestCase):
    def host(self, directory):
        host = make_host(Path(directory), emit=lambda *args: None)
        self.addCleanup(host.shutdown)
        return host

    def timer(self, host, note='Own timer'):
        # Keep production disabled while exercising native timer creation.
        host.autonomy.settings.enabled = True
        try:
            return host.autonomy.set_timer(host.slug, note, in_seconds=3600, repeat_seconds=600)
        finally:
            host.autonomy.settings.enabled = False

    def test_switch_retains_full_state_learning_outputs_floor_and_restores_owner(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        host = self.host(temp)
        setups = AutonomySetups(host)
        floor = default_definition()
        floor['rules'] = [
            {'id': 'first-example', 'name': 'First example', 'enabled': True, 'signal': 'notice',
             'when': 'true', 'template': '{message}', 'score': 1.0, 'wake': True},
            {'id': 'second-example', 'name': 'Second example', 'enabled': True, 'signal': 'notice',
             'when': 'true', 'template': '{message}', 'score': 1.0, 'wake': True},
        ]
        floor['rules'].reverse()
        floor['rules'][0]['enabled'] = False
        floor['compose']['world_view'] = 'Keep this complete world and floor'
        save_definition(host.config, floor)
        timer = self.timer(host)
        old = host.autonomy
        old.post(host.slug, 'timer', 'A held event belongs to this setup')
        old.state['endpoint_health']['owned'] = {'healthy': True}
        old.learning.record_lesson(host.slug, 'When a retry fails again', 'failure', 'verify result')
        output = Path(temp) / 'original-task.log'
        output.write_text('Retain complete task output\n')
        old.state['tasks']['bg-test'] = {'id': 'bg-test', 'slug': host.slug,
                                        'state': 'done', 'output': str(output)}
        old._save()
        original_state = old.path.read_bytes()
        original_floor = load_definition(host.config)
        original_audit = audit_path(host.config).read_bytes()
        original_learning = old.learning.path.read_bytes()
        saved = setups.save('Reusable definition')['saved_id']
        reset = setups.reset()
        retained = next(row['id'] for row in reset['setups'] if row['name'] == 'Previous setup')
        self.assertEqual(old.path.read_bytes(), original_state)
        self.assertEqual(old.learning.path.read_bytes(), original_learning)
        self.assertEqual(output.read_text(), 'Retain complete task output\n')
        self.assertEqual(host.autonomy.state['timers'], {})
        self.assertEqual(host.autonomy.state['held'], {})
        self.assertEqual(host.autonomy.learning._lessons, {})
        self.assertIsNot(host.autonomy, old)
        result = setups.select(retained)
        self.assertEqual(result['active_id'], retained)
        self.assertEqual(result['snapshot']['settings_issue'], '')
        self.assertEqual(load_definition(host.config)['rules'], original_floor['rules'])
        self.assertEqual(host.config.world_view, floor['compose']['world_view'])
        self.assertIn(timer['id'], host.autonomy.state['timers'])
        self.assertIn('owned', host.autonomy.state['endpoint_health'])
        self.assertTrue(host.autonomy.learning._lessons)
        copied = Path(host.autonomy.state['tasks']['bg-test']['output'])
        self.assertEqual(copied.parent, host.autonomy.tasks_dir)
        self.assertEqual(copied.read_text(), output.read_text())
        self.assertTrue(audit_path(host.config).read_bytes().startswith(original_audit))
        self.assertIn(saved, {row['id'] for row in setups.list()['templates']})

    def test_shared_template_instantiates_independent_live_state_and_restores_own_due(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        shared = Path(temp) / 'app'
        first = self.host(Path(temp) / 'one')
        second = self.host(Path(temp) / 'two')
        first.application_home = shared
        second.application_home = shared
        self.timer(first, 'Shared schedule definition')
        first.autonomy.post(first.slug, 'timer', 'Private outgoing wake')
        template = AutonomySetups(first).save('Shared')['saved_id']
        one = AutonomySetups(first)
        two = AutonomySetups(second)
        one.select(template)
        two.select(template)
        self.assertNotEqual(first.autonomy.path, second.autonomy.path)
        self.assertEqual(first.autonomy.state['held'], {})
        self.assertEqual(second.autonomy.state['held'], {})
        first_timer = next(iter(first.autonomy.state['timers'].values()))
        second_timer = next(iter(second.autonomy.state['timers'].values()))
        self.assertNotEqual(first_timer['id'], second_timer['id'])
        first_timer['due'] += 123
        first.autonomy._save()
        due = first_timer['due']
        one.reset()
        one.select(template)
        restored = next(iter(first.autonomy.state['timers'].values()))
        self.assertEqual(restored['id'], first_timer['id'])
        self.assertEqual(restored['due'], due)
        self.assertNotEqual(second_timer['due'], due)

    def test_pending_and_approved_actions_finish_before_switch_without_lost_closure(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        host = self.host(temp)
        setups = AutonomySetups(host)
        entered = threading.Event()
        release = threading.Event()
        def work():
            entered.set()
            release.wait(5)
            return 'Finished action'
        pending = host.autonomy.request_approval(SimpleNamespace(slug=host.slug, session=host.session), 'example', {}, work)
        identifier = pending['id']
        original = host.autonomy
        with self.assertRaisesRegex(RuntimeError, 'jobs and approvals'):
            setups.reset()
        self.assertIn(identifier, original._runs)
        with patch.object(original, '_log_result'):
            original.resolve(identifier, True)
        self.assertTrue(entered.wait(2))
        thread = original._approval_threads[identifier]
        try:
            with self.assertRaisesRegex(RuntimeError, 'jobs and approvals'):
                setups.reset()
            self.assertIs(host.autonomy, original)
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(original.active_actions())
        setups.reset()
        self.assertIsNot(host.autonomy, original)

    def test_running_job_prevents_switch_without_cancelling_process(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        host = self.host(temp)
        process = subprocess.Popen(['sleep', '30'])
        self.addCleanup(process.wait)
        self.addCleanup(process.terminate)
        original = host.autonomy
        original._children['bg-live'] = process
        original.state['tasks']['bg-live'] = {
            'id': 'bg-live', 'state': 'running', 'pid': process.pid,
        }
        with self.assertRaisesRegex(RuntimeError, 'jobs and approvals'):
            AutonomySetups(host).reset()
        self.assertIsNone(process.poll())
        self.assertIs(host.autonomy, original)
        original.state['tasks'].clear()
        original._children.clear()

    def test_restart_preserves_approved_record_as_interrupted_without_running_it(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        host = self.host(temp)
        original = host.autonomy
        original.state['approvals']['ap-restart'] = {
            'id': 'ap-restart', 'slug': host.slug, 'tool': 'example',
            'arguments': '{}', 'state': 'approved', 'created': original.clock(),
            'result': None,
        }
        original._save()
        restored = Autonomy(host._manager)
        approval = restored.state['approvals']['ap-restart']
        self.assertEqual(approval['state'], 'approved')
        self.assertEqual(approval['result'], 'interrupted')
        self.assertEqual(restored._runs, {})
        self.assertEqual(restored._approval_threads, {})
        self.assertEqual(restored.active_actions(), [])

    def test_enabled_template_restores_tools_bound_to_new_owner_and_preserves_stop(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        host = self.host(temp)
        values = host.config.settings_dict()
        values['advanced']['autonomy'] = {'enabled': True, 'standing_floor': False,
                                           'self_prompt_enabled': False, 'tick_seconds': 3600}
        host.save_settings(values, autonomy_switch='operator')
        timer = host.autonomy.set_timer(host.slug, 'New owner timer', in_seconds=3600)
        setups = AutonomySetups(host)
        template = setups.save('Enabled schedule')['saved_id']
        setups.reset()
        # Reset replaces the floor and schedule. The on/off switch is the
        # operator's, so it survives the reset.
        self.assertTrue(host.config.autonomy.enabled)
        self.assertIn('list_timers', host.session.tools.names())
        host.operator_stopped = True
        selected = setups.select(template)
        self.assertEqual(selected['snapshot']['settings_issue'], '')
        self.assertTrue(host.operator_stopped)
        self.assertTrue(host.autonomy.thread.is_alive())
        new_timer = next(iter(host.autonomy.state['timers']))
        self.assertNotEqual(new_timer, timer['id'])
        listed = host.session.tools.execute('list_timers', {})
        self.assertIn(new_timer, json.dumps(listed))

    def test_failed_first_instantiation_does_not_reuse_partial_execution_state(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        host = self.host(temp)
        self.timer(host)
        setups = AutonomySetups(host)
        template = setups.save('Retry preparation')['saved_id']
        partial = setups.instances / template / 'state.json'
        partial.parent.mkdir(parents=True)
        partial.write_text(json.dumps({'timers': {'partial': {'note': 'Uncommitted'}}}))
        setups.select(template)
        self.assertEqual(len(host.autonomy.state['timers']), 1)
        self.assertNotIn('partial', host.autonomy.state['timers'])

    def test_reset_keeps_operator_stop_unrelated_settings_and_rejects_stale_revision(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        host = self.host(temp)
        values = host.config.settings_dict()
        values['agent_name'] = 'Keep personality name'
        values['user_instructions'] = 'Keep personality text'
        values['advanced']['autonomy'] = {'max_open_lessons': 23}
        host.save_settings(values)
        host.operator_stopped = True
        setups = AutonomySetups(host)
        before = host.config.settings_dict()
        with self.assertRaisesRegex(RuntimeError, 'changed since'):
            setups.reset(expected_revision=host._settings_revision - 1)
        self.assertEqual(host.config.settings_dict(), before)
        response = setups.reset(expected_revision=host._settings_revision)
        self.assertEqual(response['snapshot']['settings_issue'], '')
        self.assertTrue(host.operator_stopped)
        self.assertEqual(host.config.agent_name, 'Keep personality name')
        self.assertEqual(host.config.user_instructions, 'Keep personality text')
        self.assertNotEqual(host.autonomy.settings.max_open_lessons, 23)

    def test_failed_precommit_restores_original_owner_and_postcommit_issue_keeps_new_owner(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        host = self.host(temp)
        setups = AutonomySetups(host)
        old = host.autonomy
        config = host.config
        with patch.object(host, 'save_settings', side_effect=OSError('Before commit')):
            with self.assertRaisesRegex(OSError, 'Before commit'):
                setups.reset()
        self.assertIs(host.config, config)
        self.assertIs(host.autonomy, old)
        with patch.object(host.voice, 'refresh_settings', side_effect=RuntimeError('Voice unavailable')):
            response = setups.reset()
        self.assertIsNot(host.autonomy, old)
        self.assertIn('Voice unavailable', response['snapshot']['settings_issue'])
        self.assertEqual(response['active_id'], Path(host.config.autonomy.state_file).parent.name)
        self.assertFalse(host.autonomy._production_paused)


if __name__ == '__main__':
    unittest.main()
