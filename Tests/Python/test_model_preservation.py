"""Saved local and account routes retain independent model options."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

from goblin_core.config import ConfigError, default_settings, load_config, normalize_settings, write_settings

ROOT = Path(__file__).resolve().parents[2]


class ModelPreservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.manifest_path = ROOT / 'product.json'
        self.manifest = json.loads(self.manifest_path.read_text())
        self.values = default_settings(self.manifest)
        self.values.update(model='fixture-local', model_base_url='http://127.0.0.1:12345/v1',
                           api_kind='responses', reasoning='medium', thinking_budget=512)

    def save(self):
        write_settings(self.home, self.manifest_path, self.values)
        return load_config(self.home, self.manifest_path)

    def test_save_and_reload_preserve_local_and_each_account(self):
        local = self.save().model_binding()
        for provider in self.manifest['oauth']:
            self.values['model_connection'] = provider
            self.values['account_models'][provider] = {
                'model': 'fixture-' + provider, 'reasoning': 'high', 'thinking_budget': 4096}
            current = self.save()
            self.assertEqual(current.model_binding()[2], 'fixture-' + provider)
            self.assertEqual(current.model_binding()[6:], ('high', 4096))
            self.assertEqual(current.model, 'fixture-local')
        self.values['model_connection'] = 'custom'
        self.assertEqual(self.save().model_binding(), local)
        for provider in self.manifest['oauth']:
            self.values['model_connection'] = provider
            self.assertEqual(self.save().model_binding()[2], 'fixture-' + provider)

    def test_account_with_no_choice_never_borrows_a_local_model(self):
        for provider in self.manifest['oauth']:
            self.values['model_connection'] = provider
            self.assertEqual(self.save().model_binding()[2], '')

    def test_inherited_local_model_does_not_cross_to_cloud(self):
        self.values['advanced'] = {'compactor': {
            'connection': '', 'inherit_model': 'fixture-local-summary', 'account_models': {}}}
        self.assertEqual(self.save().model_binding(compaction=True)[2], 'fixture-local-summary')
        self.values['model_connection'] = 'xai'
        self.values['account_models']['xai'] = {'model': 'fixture-cloud'}
        self.assertEqual(self.save().model_binding(compaction=True)[2], 'fixture-cloud')
        self.values['advanced']['compactor']['account_models']['xai'] = {'model': 'fixture-cloud-summary'}
        self.assertEqual(self.save().model_binding(compaction=True)[2], 'fixture-cloud-summary')
        self.values['model_connection'] = 'custom'
        self.assertEqual(self.save().model_binding(compaction=True)[2], 'fixture-local-summary')

    def test_explicit_local_compression_stays_local_when_chat_changes(self):
        self.values['advanced'] = {'compactor': {
            'connection': 'custom', 'model': 'fixture-local-summary',
            'model_base_url': 'http://127.0.0.1:12346/v1', 'api_kind': 'responses'}}
        original = self.save().model_binding(compaction=True)
        self.values['model_connection'] = 'xai'
        self.values['account_models']['xai'] = {'model': 'fixture-cloud'}
        self.assertEqual(self.save().model_binding(compaction=True), original)
        self.values['advanced']['compactor']['connection'] = 'xai'
        self.values['advanced']['compactor']['account_models'] = {'xai': {'model': 'fixture-cloud-summary'}}
        self.assertEqual(self.save().model_binding(compaction=True)[2], 'fixture-cloud-summary')
        self.values['advanced']['compactor']['connection'] = 'custom'
        self.assertEqual(self.save().model_binding(compaction=True), original)

    def test_new_inheritance_ignores_dormant_custom_endpoint(self):
        self.values['advanced'] = {'compactor': {
            'connection': '', 'model': 'fixture-dormant-summary',
            'model_base_url': 'http://127.0.0.1:12346/v1', 'api_kind': 'chat_completions',
            'account_models': {}}}
        inherited = self.save().model_binding(compaction=True)
        self.assertEqual(inherited, self.save().model_binding())

    def test_legacy_cloud_model_and_options_are_copied_once(self):
        self.values.pop('account_models')
        self.values['model_connection'] = 'xai'
        normalized = normalize_settings(self.values, self.manifest, self.home)
        self.assertEqual(normalized['account_models']['xai']['model'], 'fixture-local')
        self.assertEqual(normalized['account_models']['xai']['reasoning'], 'medium')
        self.assertEqual(normalized['model'], 'fixture-local')
        self.assertEqual(normalize_settings(normalized, self.manifest, self.home), normalized)
        normalized['account_models']['xai']['model'] = ''
        self.assertEqual(normalize_settings(normalized, self.manifest, self.home)['account_models']['xai']['model'], '')

    def test_legacy_separate_endpoint_becomes_explicit_and_keeps_protocol(self):
        self.values['advanced'] = {'compactor': {
            'model': 'fixture-summary', 'model_base_url': 'http://127.0.0.1:12346/v1'}}
        current = self.save()
        self.assertEqual(current.compactor.connection, 'custom')
        self.assertEqual(current.model_binding(compaction=True)[1:4],
                         ('http://127.0.0.1:12346/v1', 'fixture-summary', 'responses'))

    def test_invalid_account_choice_is_rejected_before_write(self):
        for invalid in ['not-an-object', {'model': 5}, {'reasoning': 'invalid'},
                        {'thinking_budget': -1}, {'endpoint': 'https://example.invalid'}]:
            values = copy.deepcopy(self.values)
            values['account_models'] = {'xai': invalid}
            with self.subTest(value=invalid):
                with self.assertRaises(ConfigError):
                    normalize_settings(values, self.manifest, self.home)


if __name__ == '__main__':
    unittest.main()
