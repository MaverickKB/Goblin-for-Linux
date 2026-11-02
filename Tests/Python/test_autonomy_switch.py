"""The autonomy switch belongs to the operator; a declared condition may close it."""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.floor_definition import FloorDefinitionError, default_definition, validate_definition
from test_host import make_host


def stand_down_rule():
    return {
        "id": "finished-work",
        "name": "Finished work",
        "enabled": True,
        "signal": "task",
        "when": "state == 'done'",
        "template": "The work this install was started for is finished.",
        "score": 1.0,
        "wake": False,
        "action": "stand_down",
    }


def finished_task(slug):
    ended = time.time() - 1
    return {
        "id": "task-1", "slug": slug, "label": "ship the audit", "command": "true",
        "state": "done", "started": ended - 120, "ended": ended, "exit_code": 0,
    }


class AutonomySwitchTests(unittest.TestCase):
    def wait_until_disabled(self, host, seconds=5):
        deadline = time.time() + seconds
        while host.config.autonomy.enabled and time.time() < deadline:
            time.sleep(0.1)
        return not host.config.autonomy.enabled

    def test_a_matching_condition_closes_the_switch_and_records_why(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            try:
                definition = default_definition()
                definition["rules"] = [stand_down_rule()]
                host.floor_save(json.dumps(definition))
                host.set_autonomy(True)
                self.assertTrue(host.config.autonomy.enabled)
                host.autonomy.state["tasks"] = {"task-1": finished_task(host.slug)}
                self.assertTrue(self.wait_until_disabled(host))
                self.assertEqual(host.autonomy.state["last_stand_down"]["rule"], "finished-work")
                saved = json.loads((Path(temp) / "config.json").read_text(encoding="utf-8"))
                self.assertFalse(saved["advanced"]["autonomy"]["enabled"])
            finally:
                host.shutdown()

    def test_a_later_run_can_stand_down_again(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            try:
                definition = default_definition()
                definition["rules"] = [stand_down_rule()]
                host.floor_save(json.dumps(definition))
                host.autonomy.state["tasks"] = {"task-1": finished_task(host.slug)}
                host.set_autonomy(True)
                self.assertTrue(self.wait_until_disabled(host))
                host.set_autonomy(True)
                self.assertTrue(self.wait_until_disabled(host))
            finally:
                host.shutdown()

    def test_a_save_that_is_not_the_operator_cannot_start_autonomy(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            try:
                self.assertFalse(host.config.autonomy.enabled)
                values = host.config.settings_dict()
                values["advanced"].setdefault("autonomy", {})["enabled"] = True
                host.save_settings(values)
                self.assertFalse(host.config.autonomy.enabled)
                host.save_settings(values, autonomy_switch='operator')
                self.assertTrue(host.config.autonomy.enabled)
            finally:
                host.shutdown()

    def test_a_stand_down_path_cannot_start_autonomy(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            try:
                with self.assertRaises(ValueError):
                    host.set_autonomy(True, actor="floor")
                self.assertFalse(host.config.autonomy.enabled)
                host.set_autonomy(False, actor="floor")
                self.assertFalse(host.config.autonomy.enabled)
            finally:
                host.shutdown()

    def test_recalling_a_template_leaves_the_switch_where_the_operator_put_it(self):
        with tempfile.TemporaryDirectory() as temp:
            from goblin_core.autonomy_setups import AutonomySetups
            host = make_host(temp)
            try:
                host.set_autonomy(True)
                self.assertTrue(host.config.autonomy.enabled)
                setups = AutonomySetups(host)
                saved = setups.save("Working setup")
                template_id = saved["saved_id"]
                host.set_autonomy(False)
                setups.select(template_id)
                self.assertFalse(host.config.autonomy.enabled)
                host.set_autonomy(True)
                setups.select(template_id)
                self.assertTrue(host.config.autonomy.enabled)
            finally:
                host.shutdown()

    def test_a_stand_down_rule_must_not_wake(self):
        definition = default_definition()
        rule = stand_down_rule()
        rule["wake"] = True
        definition["rules"] = [rule]
        with self.assertRaises(FloorDefinitionError):
            validate_definition(definition)
        rule["wake"] = False
        rule["action"] = "run_away"
        with self.assertRaises(FloorDefinitionError):
            validate_definition(definition)


if __name__ == "__main__":
    unittest.main()
