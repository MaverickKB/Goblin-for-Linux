"""Reusable custom model routes preserve canonical active role settings."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Runtime'))
from goblin_core.config import ConfigError, default_settings, load_config, normalize_settings, write_settings

MANIFEST = ROOT / 'product.json'


def manifest():
    return json.loads(MANIFEST.read_text())


def profiles():
    return {
        '58CB7CE2-E8DA-4BDE-A6F0-F256F680ACD0': {
            'name': 'Local coding model', 'model_base_url': 'http://127.0.0.1:8680/v1',
            'model': 'local-coder', 'api_kind': 'chat_completions', 'cli_command': '',
            'reasoning': 'high', 'thinking_budget': 24000,
        },
        'local-fast': {
            'name': 'Local summary model', 'model_base_url': 'http://127.0.0.1:8681/v1',
            'model': 'local-summary', 'api_kind': 'responses', 'cli_command': '',
            'reasoning': 'off', 'thinking_budget': 0,
        },
    }


class CustomModelPresetTests(unittest.TestCase):
    def test_two_profiles_roundtrip_without_changing_active_roles(self):
        with tempfile.TemporaryDirectory() as temp:
            values = default_settings(manifest())
            values['model'] = 'current-chat'
            values['model_base_url'] = 'http://127.0.0.1:9000/v1'
            values['advanced'] = {'compactor': {'model': 'current-summary',
                                               'model_base_url': 'http://127.0.0.1:9001/v1'}}
            before = write_settings(temp, MANIFEST, values)
            binding = before.model_binding()
            summary_binding = before.model_binding(compaction=True)
            values = before.settings_dict()
            values['custom_models'] = profiles()
            write_settings(temp, MANIFEST, values)
            saved = load_config(temp, MANIFEST)
            self.assertEqual(saved.custom_models, profiles())
            self.assertEqual(saved.settings_dict()['custom_models'], profiles())
            self.assertEqual(saved.model_binding(), binding)
            self.assertEqual(saved.model_binding(compaction=True), summary_binding)
            self.assertEqual(saved.account_models, before.account_models)
            self.assertEqual(saved.compactor.account_models, before.compactor.account_models)
            exported = saved.settings_dict()
            exported['custom_models']['local-fast']['name'] = 'Changed export'
            self.assertEqual(saved.custom_models['local-fast']['name'], 'Local summary model')

    def test_presets_preserve_account_model_choices(self):
        with tempfile.TemporaryDirectory() as temp:
            product = manifest()
            provider = next(iter(product['oauth']))
            values = default_settings(product)
            values['model_connection'] = provider
            values['account_models'] = {
                provider: {'model': 'account-chat-model', 'reasoning': 'medium', 'thinking_budget': 8000},
            }
            before = normalize_settings(values, product, temp)
            values['custom_models'] = profiles()
            after = normalize_settings(values, product, temp)
            self.assertEqual(after['account_models'], before['account_models'])
            self.assertEqual(after['model_connection'], provider)
            self.assertEqual(after['model'], before['model'])
            self.assertEqual(after['model_base_url'], before['model_base_url'])

    def test_omitted_presets_and_incomplete_saved_routes_are_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            values = default_settings(manifest())
            self.assertEqual(values['custom_models'], {})
            values.pop('custom_models')
            self.assertEqual(normalize_settings(values, manifest(), temp)['custom_models'], {})
            values['custom_models'] = {'draft': {'name': 'Draft local endpoint'},
                                       'command': {'name': 'CLI model', 'api_kind': 'cli_worker',
                                                   'cli_command': 'model-worker --stdio'}}
            saved = normalize_settings(values, manifest(), temp)['custom_models']
            self.assertEqual(saved['draft']['model'], '')
            self.assertEqual(saved['draft']['model_base_url'], '')
            self.assertEqual(saved['command']['api_kind'], 'cli_worker')

    def test_invalid_profiles_are_rejected(self):
        invalid = [None, [], {'bad/id': {'name': 'Bad ID'}}, {'draft': []},
                   {'draft': {'name': ''}}, {'draft': {'name': 5}},
                   {'draft': {'name': 'Draft', 'unknown': True}},
                   {'draft': {'name': 'Draft', 'api_kind': 'other'}},
                   {'draft': {'name': 'Draft', 'model_base_url': 'file:///tmp/model'}},
                   {'draft': {'name': 'Draft', 'model': 123}},
                   {'draft': {'name': 'Draft', 'cli_command': []}},
                   {'draft': {'name': 'Draft', 'reasoning': 'max'}},
                   {'draft': {'name': 'Draft', 'thinking_budget': True}},
                   {'draft': {'name': 'Draft', 'thinking_budget': -1}},
                   {'draft': {'name': 'Draft', 'thinking_budget': 2000001}}]
        with tempfile.TemporaryDirectory() as temp:
            for item in invalid:
                with self.subTest(profile=item):
                    values = default_settings(manifest())
                    values['custom_models'] = item
                    with self.assertRaises(ConfigError):
                        normalize_settings(values, manifest(), temp)

    def test_secret_fields_and_credential_urls_cannot_be_persisted(self):
        invalid = [ {'api_key': 'private'}, {'password': 'private'},
                    {'access_token': 'private'}, {'credentials': {'value': 'private'}},
                    {'model_base_url': 'https://user:pass@example.invalid/v1'},
                    {'model_base_url': 'https://example.invalid/v1?token=private'} ]
        with tempfile.TemporaryDirectory() as temp:
            for extra in invalid:
                with self.subTest(fields=list(extra)):
                    values = default_settings(manifest())
                    value = {'name': 'Secret-bearing profile'}
                    value.update(extra)
                    values['custom_models'] = {'draft': value}
                    with self.assertRaises(ConfigError):
                        write_settings(temp, MANIFEST, values)
                    self.assertFalse((Path(temp) / 'config.json').exists())


if __name__ == '__main__':
    unittest.main()
