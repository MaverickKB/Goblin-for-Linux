// Tool enablement. Approval rules are shown as the engine reports them.

import { request } from './api.js';

const list = document.getElementById('tools-list');
const status = document.getElementById('tools-status');
const saveButton = document.getElementById('tools-save');

let snapshot = null;
let rows = [];

function shortDescription(text) {
  const value = String(text || '');
  return value.length > 140 ? value.slice(0, 137) + '...' : value;
}

function renderRow(tool) {
  const row = document.createElement('label');
  row.className = 'tool-row';

  const toggle = document.createElement('input');
  toggle.type = 'checkbox';
  toggle.checked = Boolean(tool.enabled);
  toggle.dataset.name = tool.name;

  const copy = document.createElement('div');
  copy.className = 'tool-copy';
  const name = document.createElement('strong');
  name.textContent = tool.name;
  const description = document.createElement('small');
  description.textContent = shortDescription(tool.description);
  copy.append(name, description);

  const rule = document.createElement('span');
  rule.className = 'tool-rule';
  rule.textContent = tool.approval_rule === 'allow' ? 'Allowed' : 'Asks';
  if (tool.risky) {
    rule.textContent += ' / risky';
  }

  row.append(toggle, copy, rule);
  rows.push({ row, toggle, name: tool.name });
  return row;
}

export function renderTools(next) {
  snapshot = next;
  if (!list) {
    return;
  }
  list.textContent = '';
  rows = [];
  const tools = (next && next.tools) || [];
  if (tools.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'muted';
    empty.textContent = 'No tools are registered.';
    list.append(empty);
    return;
  }
  for (const tool of tools) {
    list.append(renderRow(tool));
  }
}

export function initTools() {
  saveButton.addEventListener('click', async () => {
    if (!snapshot) {
      return;
    }
    saveButton.disabled = true;
    status.textContent = 'Saving';
    const settings = JSON.parse(JSON.stringify(snapshot.settings || {}));
    const advanced = settings.advanced || {};
    const tools = Object.assign({}, advanced.tools || {});
    const disabled = [];
    for (const item of rows) {
      if (!item.toggle.checked) {
        disabled.push(item.name);
      }
    }
    tools.disabled = disabled;
    advanced.tools = tools;
    settings.advanced = advanced;
    try {
      const saved = await request('settings/save', {
        settings,
        expected_revision: snapshot.settings_revision,
      });
      snapshot = saved;
      status.textContent = saved.settings_issue || 'Saved';
    } catch (error) {
      status.textContent = error.message;
    } finally {
      saveButton.disabled = false;
    }
  });
}
