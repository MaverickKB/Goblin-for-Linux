// Settings form. The engine validates and owns every saved value.

import { request } from './api.js';

const form = document.getElementById('settings-form');
const status = document.getElementById('settings-status');
const agentName = document.getElementById('agent-name');
const instructions = document.getElementById('user-instructions');
const baseUrl = document.getElementById('model-base-url');
const model = document.getElementById('model');
const apiKind = document.getElementById('api-kind');

let snapshot = null;
let saving = false;
let onSaved = null;

export function fill(nextSettings) {
  if (!nextSettings) {
    return;
  }
  agentName.value = nextSettings.agent_name || '';
  instructions.value = nextSettings.user_instructions || '';
  baseUrl.value = nextSettings.model_base_url || '';
  model.value = nextSettings.model || '';
  apiKind.value = nextSettings.api_kind || 'chat_completions';
}

export function applySnapshot(next, options = {}) {
  const first = snapshot === null;
  snapshot = next;
  if (first || options.force) {
    fill(next.settings);
  }
}

function setStatus(text, tone) {
  status.textContent = text;
  status.style.color = tone === 'error' ? 'var(--danger)' : 'var(--muted)';
}

async function save() {
  if (saving || !snapshot) {
    return;
  }
  saving = true;
  setStatus('Saving');
  const settings = JSON.parse(JSON.stringify(snapshot.settings || {}));
  settings.agent_name = agentName.value.trim() || 'Goblin';
  settings.user_instructions = instructions.value;
  settings.model_base_url = baseUrl.value.trim();
  settings.model = model.value.trim();
  settings.api_kind = apiKind.value;
  try {
    const next = await request('settings/save', {
      settings,
      expected_revision: snapshot.settings_revision,
    });
    applySnapshot(next, { force: true });
    if (next.settings_issue) {
      setStatus(next.settings_issue, 'error');
    } else {
      setStatus('Saved');
    }
    if (onSaved) {
      onSaved(next);
    }
  } catch (error) {
    setStatus(error.message, 'error');
  } finally {
    saving = false;
  }
}

export function initSettings(options = {}) {
  onSaved = options.onSaved || null;
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    save();
  });
}
