"""Idle floor wakes follow the floor interval. Wake-now is the forced path."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Runtime"))

from goblin_core.autonomy_runtime import SessionRuntimeProxy
from goblin_core.floor_definition import default_definition, save_definition
from test_host import make_host


def _install_clock(host, now):
    host.autonomy.clock = lambda: now[0]


def _silence_turns(host):
    original = SessionRuntimeProxy.enqueue

    def enqueue(self, text, *args, **kwargs):
        return "turn-test"

    SessionRuntimeProxy.enqueue = enqueue
    return original


class FloorCadenceTests(unittest.TestCase):
    def _prepare(self, host, interval, min_gap=1, per_hour=0, per_day=0):
        settings = host.autonomy.settings
        settings.enabled = True
        settings.standing_floor = True
        settings.self_prompt_enabled = False
        settings.self_prompt_gap_seconds = interval
        settings.min_wake_gap_seconds = min_gap
        settings.max_wakes_per_hour = per_hour
        settings.max_wakes_per_day = per_day
        settings.tick_seconds = 3600
        host.autonomy.close()

    def test_background_ticks_wake_about_once_per_floor_interval(self):
        interval = 200
        minimum_gap = 60
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            original = _silence_turns(host)
            try:
                self._prepare(host, interval, min_gap=minimum_gap)
                now = [0.0]
                _install_clock(host, now)
                steps = 0
                while now[0] <= interval * 3:
                    host.autonomy.tick()
                    now[0] += 10
                    steps += 1
                wakes = host.autonomy.state["wakes"]
                self.assertEqual(len(wakes), 3, wakes)
                gaps = [wakes[i]["at"] - wakes[i - 1]["at"] for i in range(1, len(wakes))]
                self.assertEqual(gaps, [interval, interval])
                self.assertGreater(len(wakes), 0)
                self.assertLess(len(wakes), steps // (minimum_gap // 10))
            finally:
                SessionRuntimeProxy.enqueue = original
                host.shutdown()

    def test_forced_wake_composes_before_the_interval(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            original = _silence_turns(host)
            try:
                self._prepare(host, 900, min_gap=0)
                now = [0.0]
                _install_clock(host, now)
                self.assertIsNone(host.autonomy.tick())
                self.assertEqual(host.autonomy.state["wakes"], [])
                now[0] = 1
                self.assertTrue(host.autonomy.tick(force_floor=True))
                self.assertEqual(len(host.autonomy.state["wakes"]), 1)
                self.assertEqual(host.autonomy.state["wakes"][0]["at"], 1)
            finally:
                SessionRuntimeProxy.enqueue = original
                host.shutdown()

    def test_hourly_and_daily_caps_block_when_set_and_not_when_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            original = _silence_turns(host)
            try:
                self._prepare(host, 10, min_gap=0, per_hour=1, per_day=0)
                now = [0.0]
                _install_clock(host, now)
                self.assertTrue(host.autonomy.tick(force_floor=True))
                now[0] = 11
                self.assertIsNone(host.autonomy.tick(force_floor=True))
                self.assertEqual(len(host.autonomy.state["wakes"]), 1)
                slug = host.autonomy.settings.floor_slug
                self.assertEqual(host.autonomy.state["suppress"][slug], "hold:max_wakes_per_hour")

                host.autonomy.settings.max_wakes_per_hour = 0
                host.autonomy.settings.max_wakes_per_day = 1
                now[0] = 12
                self.assertIsNone(host.autonomy.tick(force_floor=True))
                self.assertEqual(host.autonomy.state["suppress"][slug], "hold:max_wakes_per_day")

                host.autonomy.settings.max_wakes_per_day = 0
                now[0] = 13
                self.assertTrue(host.autonomy.tick(force_floor=True))
                self.assertEqual(len(host.autonomy.state["wakes"]), 2)
            finally:
                SessionRuntimeProxy.enqueue = original
                host.shutdown()

    def test_pressure_wakes_are_not_held_for_the_floor_interval(self):
        interval = 3600
        minimum_gap = 60
        definition = default_definition()
        definition["rules"] = [
            {
                "id": "finished-task",
                "name": "Example finished task",
                "enabled": True,
                "signal": "task",
                "when": "state == 'done'",
                "template": "Pressure: task {label} is done.",
                "score": 1.4,
                "wake": True,
                "pressure": True,
            },
        ]

        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            posted = []
            original = SessionRuntimeProxy.enqueue

            def enqueue(self, text, *args, **kwargs):
                posted.append((host.autonomy.clock(), text))
                return "turn-test"

            SessionRuntimeProxy.enqueue = enqueue
            try:
                self._prepare(host, interval, min_gap=minimum_gap)
                save_definition(host.config, definition)
                now = [0.0]
                _install_clock(host, now)
                while now[0] <= interval * 3:
                    if now[0] == 300:
                        # The task finishes here and is pruned a minute later.
                        host.autonomy.state["tasks"]["t1"] = {
                            "id": "t1", "slug": host.slug, "label": "check the build",
                            "command": "true", "state": "done", "started": 0,
                            "ended": 300, "exit_code": 0,
                        }
                    if now[0] == 360:
                        host.autonomy.state["tasks"].pop("t1", None)
                    host.autonomy.tick()
                    now[0] += 30
                late = [stamp for stamp, text in posted if "check the build" in text]
                quiet = [stamp for stamp, text in posted if "Nothing measured" in text]
                self.assertEqual(len(late), 1, posted)
                self.assertGreaterEqual(late[0], 300)
                self.assertLessEqual(late[0] - 300, minimum_gap)
                self.assertGreaterEqual(len(quiet), 2)
                quiet_gaps = [quiet[i] - quiet[i - 1] for i in range(1, len(quiet))]
                self.assertTrue(all(gap == interval for gap in quiet_gaps), quiet)
            finally:
                SessionRuntimeProxy.enqueue = original
                host.shutdown()

    def test_zero_caps_do_not_limit_wakes_inside_an_hour(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            original = _silence_turns(host)
            try:
                self._prepare(host, 10, min_gap=0, per_hour=0, per_day=0)
                now = [0.0]
                _install_clock(host, now)
                self.assertTrue(host.autonomy.tick(force_floor=True))
                now[0] = 10
                self.assertTrue(host.autonomy.tick())
                self.assertEqual(len(host.autonomy.state["wakes"]), 2)
                self.assertLess(host.autonomy.state["wakes"][1]["at"] - host.autonomy.state["wakes"][0]["at"], 3600)
            finally:
                SessionRuntimeProxy.enqueue = original
                host.shutdown()


if __name__ == "__main__":
    unittest.main()
