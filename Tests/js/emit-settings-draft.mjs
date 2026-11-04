// Prints the settings document src/settings-draft.js composes for each case.
// The Python binding test reads this so a JS drift fails there, not silently.

import { composeSettings } from '../../src/settings-draft.js';

const cases = {
  compactor_provider: {
    provider: 'xai',
    providerModel: 'grok-4.7',
    compactorProvider: 'xai',
    compactorModel: 'grok-4.20-0309-non-reasoning',
  },
  compactor_same_as_chat: {
    provider: 'custom',
    customModel: 'chat-model',
    compactorProvider: '',
    compactorModel: '',
  },
  compactor_custom_endpoint: {
    provider: 'custom',
    customModel: 'chat-model',
    compactorProvider: 'custom',
    compactorCustomModel: 'fast-compress',
  },
  chat_provider: {
    provider: 'xai',
    providerModel: 'grok-4.7',
    compactorProvider: '',
  },
  compactor_route_changed_from_custom: {
    provider: 'custom',
    customModel: 'chat-model',
    compactorProvider: 'xai',
    compactorModel: 'grok-4.20-0309-non-reasoning',
    base: {
      advanced: {
        compactor: {
          connection: 'custom',
          model: 'stale-model',
          model_base_url: 'http://127.0.0.1:11/v1',
          api_kind: 'responses',
          workers: 3,
        },
      },
    },
  },
  compactor_provider_without_model: {
    provider: 'custom',
    customModel: 'chat-model',
    compactorProvider: 'xai',
    compactorModel: '',
  },
};

const base = {
  agent_name: 'Goblin',
  user_instructions: '',
  workspace: '',
  model_base_url: '',
  model: '',
  model_connection: 'custom',
  api_kind: 'chat_completions',
  cli_command: '',
  reasoning: 'low',
  thinking_budget: 0,
  approval: 'ask',
  world_view: '',
  floor_interval_seconds: 900,
  voice_model: '',
  voice_name: '',
  voice_auth: 'oauth',
  advanced: {},
  account_models: {},
  custom_models: {},
};

const out = {};
for (const [name, draft] of Object.entries(cases)) {
  out[name] = composeSettings({ ...base, ...(draft.base || {}) }, {
    agentName: 'Goblin',
    instructions: '',
    apiKind: 'chat_completions',
    baseUrl: '',
    customModel: '',
    compactorCustomModel: '',
    ...draft,
  });
}
process.stdout.write(JSON.stringify(out));
