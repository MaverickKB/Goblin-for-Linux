"""OAuth wire requests identify the owning product without exposing failures."""

import copy
import unittest

from test_account import MANIFEST, MemoryGrantStore, XAI_TOKEN_URL
from goblin_core import account_oauth


class OAuthHTTPIdentityTests(unittest.TestCase):
    def test_device_discovery_poll_exchange_and_refresh_use_product_identity(self):
        manifest = copy.deepcopy(MANIFEST)
        manifest['name'] = 'FixtureClient'
        manifest['version'] = '3.2.1'
        definitions = account_oauth.provider_definitions(manifest)
        calls = []

        def post(url, body, headers):
            calls.append(dict(headers))
            return 200, {
                'user_code': 'fixture-code', 'device_code': 'fixture-device',
                'device_auth_id': 'fixture-device-id',
                'verification_uri': 'https://auth.x.ai/device',
                'authorization_code': 'fixture-authorization', 'code_verifier': 'fixture-verifier',
                'access_token': 'fixture-access', 'refresh_token': 'fixture-refresh',
                'expires_in': 600,
            }

        def get(url, headers):
            calls.append(dict(headers))
            return {'token_endpoint': XAI_TOKEN_URL}

        for provider, definition in definitions.items():
            with self.subTest(provider=provider):
                device = account_oauth._request_device_code(definition, post, get)
                state, payload = account_oauth._poll_device(definition, device, post)
                self.assertEqual(state, 'approved')
                account_oauth._approval_tokens(definition, payload, post)
                store = MemoryGrantStore()
                store.definition = definition
                store.save({'access_token': 'expired', 'refresh_token': 'fixture-refresh',
                            'expires_at': 1, 'token_endpoint': XAI_TOKEN_URL})
                account_oauth.resolve_grant(store, provider=provider, poster=post, now=lambda: 1000)
        self.assertTrue(calls)
        for headers in calls:
            self.assertEqual(headers.get('User-Agent'), 'FixtureClient/3.2.1')
            self.assertEqual(headers.get('Accept'), 'application/json')

    def test_device_failure_reports_http_status_without_provider_body(self):
        definitions = account_oauth.provider_definitions(MANIFEST)

        def rejected(url, body, headers):
            return 530, {'error': 'private-provider-response'}

        for provider, definition in definitions.items():
            with self.subTest(provider=provider):
                with self.assertRaises(account_oauth.OAuthError) as caught:
                    account_oauth._request_device_code(definition, rejected, None)
                self.assertIn('HTTP 530', str(caught.exception))
                self.assertNotIn('private-provider-response', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
