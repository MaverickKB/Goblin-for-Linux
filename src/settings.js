// Settings form. The engine validates and owns every saved value.

import { request } from './api.js';
import {
  CUSTOM_PROVIDER,
  COMPACTION_MODEL,
  COMPACTION_PROVIDER,
  catalogFor,
  closestMatch,
  modelOptions,
  providerName,
} from './model-catalog.js';

import { composeSettings } from './settings-draft.js';

const saveButton = document.getElementById('settings-save');
const status = document.getElementById('settings-status');
const agentName = document.getElementById('agent-name');
const instructions = document.getElementById('user-instructions');
const baseUrl = document.getElementById('model-base-url');
const baseUrlField = document.getElementById('model-base-url-field');
const connection = document.getElementById('model-connection');
const modelSelect = document.getElementById('model');
const modelCustom = document.getElementById('model-custom');
const modelField = document.getElementById('model-field');
const modelCustomField = document.getElementById('model-custom-field');
const modelStatus = document.getElementById('model-status');
const compactorConnection = document.getElementById('compactor-connection');
const compactorModel = document.getElementById('compactor-model');
const compactorCustom = document.getElementById('compactor-custom');
const compactorModelField = document.getElementById('compactor-model-field');
const compactorCustomField = document.getElementById('compactor-custom-field');
const compactorStatus = document.getElementById('compactor-status');
const compactionActivity = document.getElementById('compaction-activity');
const apiKind = document.getElementById('api-kind');

let snapshot = null;
let saving = false;
let onSaved = null;
let providers = [];
const catalogRequests = new Map();

function setStatus(element, text, tone) {
  element.textContent = text;
  element.style.color = tone === 'error' ? 'var(--danger)' : 'var(--muted)';
}

function fillSelect(select, entries, selected) {
  const seen = entries.some((entry) => entry.value === selected);
  select.textContent = '';
  for (const entry of entries) {
    const option = document.createElement('option');
    option.value = entry.value;
    option.textContent = entry.label;
    if (entry.value === selected) {
      option.selected = true;
    }
    select.append(option);
  }
  if (!seen) {
    const option = document.createElement('option');
    option.value = selected;
    option.textContent = providerName(selected) + ' (not configured)';
    option.selected = true;
    select.append(option);
  }
}

function providerEntries(includeInherit) {
  const entries = includeInherit ? [{ value: '', label: 'Same as chat' }] : [];
  entries.push({ value: CUSTOM_PROVIDER, label: providerName(CUSTOM_PROVIDER) });
  for (const report of providers) {
    const name = providerName(report.provider);
    entries.push({
      value: report.provider,
      label: report.state === 'signed_in' ? name : name + ' (not connected)',
    });
  }
  return entries;
}

async function loadProviders() {
  try {
    const result = await request('account/list');
    providers = (result.accounts || []).filter((report) => report && report.provider);
  } catch (error) {
    providers = [];
  }
}

function loadCatalog(provider) {
  if (catalogRequests.has(provider)) {
    return catalogRequests.get(provider);
  }
  const pending = request('account/check', { provider }, 45000)
    .then((result) => {
      catalogRequests.delete(provider);
      if (!result.ok) {
        throw new Error(result.message || 'The provider catalog is unavailable.');
      }
      return result.models || [];
    })
    .catch((error) => {
      catalogRequests.delete(provider);
      throw error;
    });
  catalogRequests.set(provider, pending);
  return pending;
}

function renderModelSelect(select, provider, fetched, savedId) {
  const options = modelOptions(provider, fetched, savedId);
  select.textContent = '';
  if (options.length === 0) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = 'No models available';
    option.disabled = true;
    option.selected = true;
    select.append(option);
    return;
  }
  for (const model of options) {
    const option = document.createElement('option');
    option.value = model.id;
    option.textContent = model.saved && !model.known
      ? model.name + ' (saved, not in catalog)'
      : model.name + (model.reasoning === false ? ' - fast' : '');
    if (model.id === savedId) {
      option.selected = true;
    }
    select.append(option);
  }
}

function unknownWarning(provider, savedId) {
  if (!savedId || provider === CUSTOM_PROVIDER) {
    return '';
  }
  if (catalogFor(provider).some((model) => model.id === savedId)) {
    return '';
  }
  const suggestion = closestMatch(provider, savedId);
  return suggestion
    ? savedId + ' is not a documented ' + providerName(provider)
      + ' model. Closest match: ' + suggestion
    : savedId + ' is not a documented ' + providerName(provider) + ' model.';
}

function syncModelVisibility() {
  const custom = connection.value === CUSTOM_PROVIDER;
  modelCustomField.hidden = !custom;
  modelField.hidden = custom;
  baseUrlField.hidden = !custom;
}

async function refreshModelCatalog(savedId) {
  const provider = connection.value;
  syncModelVisibility();
  if (provider === CUSTOM_PROVIDER) {
    setStatus(modelStatus, '');
    return;
  }
  renderModelSelect(modelSelect, provider, catalogFor(provider), savedId);
  setStatus(modelStatus, 'Loading provider catalog');
  try {
    const fetched = await loadCatalog(provider);
    renderModelSelect(modelSelect, provider, fetched, savedId);
    const warning = unknownWarning(provider, savedId);
    if (warning) {
      setStatus(modelStatus, warning, 'error');
    } else {
      setStatus(modelStatus, fetched.length + ' models listed by the provider');
    }
  } catch (error) {
    setStatus(modelStatus, error.message + '. Showing documented models.', 'error');
  }
}

function syncCompactorVisibility() {
  const choice = compactorConnection.value;
  const custom = choice === CUSTOM_PROVIDER;
  compactorCustomField.hidden = !custom;
  compactorModelField.hidden = choice === '' || custom;
}

async function refreshCompactorCatalog(savedId) {
  const choice = compactorConnection.value;
  syncCompactorVisibility();
  if (choice === '') {
    setStatus(compactorStatus, 'Compaction uses the chat route.');
    return;
  }
  if (choice === CUSTOM_PROVIDER) {
    setStatus(compactorStatus, '');
    return;
  }
  renderModelSelect(compactorModel, choice, catalogFor(choice), savedId);
  setStatus(compactorStatus, 'Loading provider catalog');
  try {
    const fetched = await loadCatalog(choice);
    renderModelSelect(compactorModel, choice, fetched, savedId);
    setStatus(compactorStatus, fetched.length + ' models listed by the provider');
  } catch (error) {
    setStatus(compactorStatus, error.message + '. Showing documented models.', 'error');
  }
}

function savedModelId(settings, provider) {
  if (!settings || provider === CUSTOM_PROVIDER) {
    return '';
  }
  const slot = (settings.account_models || {})[provider] || {};
  return slot.model || '';
}

function compactorChoice(settings) {
  const compactor = ((settings && settings.advanced) || {}).compactor || {};
  return compactor.connection || '';
}

function compactorSavedModel(settings, choice) {
  const compactor = ((settings && settings.advanced) || {}).compactor || {};
  if (choice === CUSTOM_PROVIDER) {
    return compactor.model || '';
  }
  const slot = (compactor.account_models || {})[choice] || {};
  return slot.model || '';
}

export function fill(nextSettings) {
  if (!nextSettings) {
    return;
  }
  agentName.value = nextSettings.agent_name || '';
  instructions.value = nextSettings.user_instructions || '';
  baseUrl.value = nextSettings.model_base_url || '';
  apiKind.value = nextSettings.api_kind || 'chat_completions';

  const provider = nextSettings.model_connection || CUSTOM_PROVIDER;
  fillSelect(connection, providerEntries(false), provider);
  modelCustom.value = provider === CUSTOM_PROVIDER ? (nextSettings.model || '') : '';
  syncModelVisibility();
  refreshModelCatalog(savedModelId(nextSettings, provider));

  const choice = compactorChoice(nextSettings) || COMPACTION_PROVIDER;
  fillSelect(compactorConnection, providerEntries(true), choice);
  compactorCustom.value = choice === CUSTOM_PROVIDER
    ? (((nextSettings.advanced || {}).compactor || {}).model || '')
    : '';
  syncCompactorVisibility();
  refreshCompactorCatalog(compactorSavedModel(nextSettings, choice) || COMPACTION_MODEL);
}

export function applySnapshot(next, options = {}) {
  const first = snapshot === null;
  snapshot = next;
  if (first || options.force) {
    fill(next.settings);
  }
}

export function noteCompaction(state) {
  if (!state) {
    return;
  }
  const parts = [];
  if (Number.isFinite(Number(state.bytes))) {
    parts.push(Number(state.bytes).toLocaleString() + ' bytes');
  }
  if (Number.isFinite(Number(state.pending))) {
    parts.push(Number(state.pending) + ' pending summaries');
  }
  compactionActivity.textContent = parts.length ? 'Last compaction: ' + parts.join(', ') : '';
}

async function save() {
  if (saving || !snapshot) {
    return;
  }
  const provider = connection.value;
  if (provider !== CUSTOM_PROVIDER && !modelSelect.value) {
    setStatus(status, 'Pick a model for ' + providerName(provider) + ' before saving.', 'error');
    return;
  }
  const choice = compactorConnection.value;
  if (choice === CUSTOM_PROVIDER && !compactorCustom.value.trim()) {
    setStatus(status, 'Enter a model id for the custom compression endpoint.', 'error');
    return;
  }

  saving = true;
  setStatus(status, 'Saving');
  const settings = composeSettings(snapshot.settings || {}, {
    agentName: agentName.value,
    instructions: instructions.value,
    apiKind: apiKind.value,
    baseUrl: baseUrl.value,
    provider,
    providerModel: modelSelect.value,
    customModel: modelCustom.value,
    compactorProvider: choice,
    compactorModel: compactorModel.value,
    compactorCustomModel: compactorCustom.value,
  });
  try {
    const next = await request('settings/save', {
      settings,
      expected_revision: snapshot.settings_revision,
    });
    applySnapshot(next, { force: true });
    if (next.settings_issue) {
      setStatus(status, next.settings_issue, 'error');
    } else {
      setStatus(status, 'Saved');
    }
    if (onSaved) {
      onSaved(next);
    }
  } catch (error) {
    setStatus(status, error.message, 'error');
  } finally {
    saving = false;
  }
}

export function initSettings(options = {}) {
  onSaved = options.onSaved || null;
  saveButton.addEventListener('click', (event) => {
    event.preventDefault();
    save();
  });
  connection.addEventListener('change', () => {
    const provider = connection.value;
    modelCustom.value = provider === CUSTOM_PROVIDER && snapshot
      ? (snapshot.settings.model || '')
      : '';
    refreshModelCatalog(savedModelId(snapshot && snapshot.settings, provider));
  });
  compactorConnection.addEventListener('change', () => {
    const choice = compactorConnection.value;
    refreshCompactorCatalog(
      compactorSavedModel(snapshot && snapshot.settings, choice) || COMPACTION_MODEL,
    );
  });
  loadProviders().then(() => {
    const currentProvider = (snapshot && snapshot.settings
      && snapshot.settings.model_connection) || CUSTOM_PROVIDER;
    fillSelect(connection, providerEntries(false), connection.value || currentProvider);
    const choice = (snapshot && snapshot.settings
      && compactorChoice(snapshot.settings)) || COMPACTION_PROVIDER;
    fillSelect(compactorConnection, providerEntries(true), compactorConnection.value || choice);
    if (snapshot && snapshot.settings) {
      fill(snapshot.settings);
    }
  });
}
