// Turns form values into the settings document the engine validates.
// Pure so the save payload can be checked without a browser.

import { CUSTOM_PROVIDER } from './model-catalog.js';

const COMPACTOR_LIMITS = ['workers', 'retries', 'context_bytes'];

export function composeSettings(base, draft) {
  const settings = JSON.parse(JSON.stringify(base || {}));
  settings.agent_name = (draft.agentName || '').trim() || 'Goblin';
  settings.user_instructions = draft.instructions || '';
  settings.api_kind = draft.apiKind;
  settings.model_base_url = (draft.baseUrl || '').trim();

  const provider = draft.provider || CUSTOM_PROVIDER;
  settings.model_connection = provider;
  if (provider === CUSTOM_PROVIDER) {
    settings.model = (draft.customModel || '').trim();
  } else {
    settings.account_models = settings.account_models || {};
    settings.account_models[provider] = {
      ...(settings.account_models[provider] || {}),
      model: draft.providerModel,
    };
  }

  settings.advanced = settings.advanced || {};
  const previous = settings.advanced.compactor || {};
  // Route fields are rebuilt every save so a stale endpoint or model id from an
  // earlier route cannot linger. Only the sizing limits survive untouched.
  const compactor = {};
  for (const key of COMPACTOR_LIMITS) {
    if (previous[key] !== undefined) {
      compactor[key] = previous[key];
    }
  }
  const choice = draft.compactorProvider || '';
  if (choice === '') {
    settings.advanced.compactor = compactor;
  } else if (choice === CUSTOM_PROVIDER) {
    settings.advanced.compactor = {
      ...compactor,
      connection: CUSTOM_PROVIDER,
      model: (draft.compactorCustomModel || '').trim(),
    };
  } else {
    settings.advanced.compactor = {
      ...compactor,
      connection: choice,
      account_models: {
        [choice]: {
          model: draft.compactorModel,
          reasoning: 'off',
          thinking_budget: 0,
        },
      },
    };
  }
  return settings;
}
