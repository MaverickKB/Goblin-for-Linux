// Autonomy floor editing and the continuity summary.

import { request } from './api.js';

const definition = document.getElementById('floor-definition');
const status = document.getElementById('floor-status');
const saveButton = document.getElementById('floor-save');
const continuity = document.getElementById('continuity');

let revision = 0;

function describeStatus(value) {
  if (!value) {
    return '';
  }
  if (typeof value === 'string') {
    return value;
  }
  const rules = Array.isArray(value.rules) ? value.rules.length : null;
  if (rules !== null) {
    return rules + ' rules loaded';
  }
  return 'Floor loaded';
}

export async function loadFloor(snapshot) {
  revision = snapshot && snapshot.settings_revision ? snapshot.settings_revision : revision;
  try {
    const result = await request('floor/read');
    definition.value = result.definition || '';
    status.textContent = describeStatus(result.status);
  } catch (error) {
    status.textContent = error.message;
  }
}

async function saveFloor() {
  saveButton.disabled = true;
  status.textContent = 'Saving';
  try {
    const result = await request('floor/save', {
      definition: definition.value,
      expected_revision: revision,
    });
    status.textContent = describeStatus(result.status) || 'Saved';
  } catch (error) {
    status.textContent = error.message;
  } finally {
    saveButton.disabled = false;
  }
}

export function renderContinuity(memory) {
  continuity.textContent = '';
  if (!memory) {
    continuity.textContent = 'No continuity state reported.';
    return;
  }
  const rows = [
    ['State', memory.state || 'unknown'],
    ['Mode', memory.mode || 'unknown'],
    ['Project', memory.project || 'not set'],
    ['Layers', String(memory.layers || 0)],
    ['Pending writes', String(memory.debt || 0)],
  ];
  for (const [label, value] of rows) {
    const row = document.createElement('div');
    row.className = 'info-row';
    const name = document.createElement('span');
    name.textContent = label;
    const text = document.createElement('strong');
    text.textContent = value;
    row.append(name, text);
    continuity.append(row);
  }
}

export function initFloor() {
  saveButton.addEventListener('click', saveFloor);
}
