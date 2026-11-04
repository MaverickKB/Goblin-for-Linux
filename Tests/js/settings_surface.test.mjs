// Run with: node --test Tests/js
// Drives src/settings.js against a minimal DOM so the selects are proven to
// populate from the catalog, not just to parse. One document, because the
// module binds its elements once at import, as the real page does.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = new URL('../../src/', import.meta.url).pathname;
const HTML = readFileSync(join(SRC, 'index.html'), 'utf8');

class StubElement {
  constructor() {
    this.children = [];
    this.textContent = '';
    this.value = '';
    this.hidden = false;
    this.disabled = false;
    this.style = {};
    this.listeners = {};
  }
  append(...nodes) {
    this.children.push(...nodes);
  }
  replaceChildren(...nodes) {
    this.children = [...nodes];
  }
  addEventListener(name, handler) {
    this.listeners[name] = handler;
  }
  get options() {
    return this.children.filter((child) => child.isOption);
  }
}

const byId = new Map();
for (const match of HTML.matchAll(/\sid="([^"]+)"/g)) {
  byId.set(match[1], new StubElement());
}
globalThis.window = {};
globalThis.document = {
  getElementById: (id) => byId.get(id) || null,
  createElement: () => {
    const element = new StubElement();
    element.isOption = true;
    return element;
  },
};

const settings = await import('../../src/settings.js');
settings.initSettings({});

function apply(settingsDocument) {
  settings.applySnapshot({
    settings_revision: 1,
    settings: settingsDocument,
    model: '',
    autonomy: { enabled: false },
    pending_approvals: [],
    memory: null,
    tools: [],
    compaction: {},
  }, { force: true });
}

test('the model select fills with documented ids for a provider', () => {
  apply({
    agent_name: 'Goblin',
    model_connection: 'xai',
    account_models: { xai: { model: 'grok-4-7' } },
    advanced: {},
  });
  const select = byId.get('model');
  const ids = select.options.map((option) => option.value);
  assert.ok(ids.includes('grok-4.7'), 'documented ids must be listed');
  assert.ok(ids.includes('grok-4.20-0309-non-reasoning'));
  assert.ok(ids.includes('grok-4-7'), 'the saved id is kept');
  const saved = select.options.find((option) => option.value === 'grok-4-7');
  assert.match(saved.textContent, /saved, not in catalog/);
  assert.equal(select.value, 'grok-4-7');
});

test('the compression select defaults to the documented non-reasoning model', () => {
  apply({
    agent_name: 'Goblin',
    model_connection: 'custom',
    model: 'chat-model',
    advanced: {},
  });
  assert.equal(byId.get('compactor-connection').value, 'xai');
  const model = byId.get('compactor-model');
  assert.equal(model.value, 'grok-4.20-0309-non-reasoning');
  const chosen = model.options.find((option) => option.value === model.value);
  assert.match(chosen.textContent, /non-reasoning/);
});

test('a custom endpoint keeps the free-text model field', () => {
  apply({
    agent_name: 'Goblin',
    model_connection: 'custom',
    model: 'chat-model',
    advanced: {},
  });
  assert.equal(byId.get('model-custom').value, 'chat-model');
  assert.equal(byId.get('model-custom-field').hidden, false);
  assert.equal(byId.get('model-field').hidden, true);
});

test('a provider with no documented catalog says so instead of an empty list', () => {
  apply({
    agent_name: 'Goblin',
    model_connection: 'openai-codex',
    account_models: {},
    advanced: {},
  });
  const select = byId.get('model');
  assert.equal(select.value, '');
  assert.equal(select.options[0].textContent, 'No models available');
  assert.equal(select.options[0].disabled, true);
});
