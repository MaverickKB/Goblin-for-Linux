"""Fresh product configuration and native tools carry no private runtime jobs."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Runtime'))
from goblin_core.config import load_config, write_settings
from goblin_core.native_cua import register_native_tools
from goblin_core.tools import ToolRegistry
from test_host import make_host


class ProductDefaultTests(unittest.TestCase):
    def test_native_tools_register_and_settings_round_trip_without_extra_backends(self):
        with tempfile.TemporaryDirectory() as temp:
            values = {'advanced': {'computer_use': {'backend': 'native'}}}
            with patch('urllib.request.urlopen', side_effect=AssertionError('No external judge')):
                config = write_settings(Path(temp), ROOT / 'product.json', values)
                registry = ToolRegistry()
                names = register_native_tools(registry, config)
                try:
                    self.assertIn('computer_get_app_state', names)
                    self.assertIn('computer_click', names)
                    self.assertNotIn('computer_find_element', names)
                    self.assertNotIn('computer_check', names)
                finally:
                    registry.release()
            again = load_config(Path(temp), ROOT / 'product.json')
            self.assertEqual(again.settings_dict()['advanced']['computer_use'], {'backend': 'native'})

    def test_fresh_floor_has_no_personal_watch_job_or_automatic_host_probe(self):
        with tempfile.TemporaryDirectory() as temp:
            host = make_host(temp)
            try:
                self.assertEqual(host.config.world_view, '')
                self.assertFalse(host.autonomy.settings.enabled)
                definition = host.autonomy._floor_reading(0)['definition']
                self.assertEqual(definition['rules'], [])
            finally:
                host.shutdown()


if __name__ == '__main__':
    unittest.main()
