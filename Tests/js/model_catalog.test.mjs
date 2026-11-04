// Run with: node --test Tests/js

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  COMPACTION_MODEL,
  COMPACTION_PROVIDER,
  catalogFor,
  closestMatch,
  modelOptions,
  providerName,
} from '../../src/model-catalog.js';

const DOCUMENTED_XAI = [
  'grok-4.7',
  'grok-4.6',
  'grok-4.5',
  'grok-4.3',
  'grok-4.20-0309-reasoning',
  'grok-4.20-0309-non-reasoning',
  'grok-4.20-multi-agent-0309',
  'grok-build-0.1',
];

test('the xai catalog holds every documented model id', () => {
  const ids = catalogFor('xai').map((model) => model.id);
  assert.deepEqual(ids, DOCUMENTED_XAI);
});

test('the compaction default is the documented non-reasoning model', () => {
  assert.equal(COMPACTION_PROVIDER, 'xai');
  assert.equal(COMPACTION_MODEL, 'grok-4.20-0309-non-reasoning');
  const entry = catalogFor('xai').find((model) => model.id === COMPACTION_MODEL);
  assert.ok(entry, 'compaction default must be in the catalog');
  assert.equal(entry.reasoning, false);
});

test('a fetched catalog merges over the documented list without duplicates', () => {
  const options = modelOptions('xai', [
    { id: 'grok-4.7', name: 'Grok 4.7' },
    { id: 'grok-9-preview', name: 'Grok 9 preview' },
  ], 'grok-4.7');
  const ids = options.map((option) => option.id);
  assert.equal(ids.filter((id) => id === 'grok-4.7').length, 1);
  assert.ok(ids.includes('grok-9-preview'));
  assert.equal(options.find((option) => option.id === 'grok-4.7').reasoning, true);
});

test('a saved id absent from the catalog is kept and marked', () => {
  const options = modelOptions('xai', [{ id: 'grok-4.7' }], 'grok-4-7');
  const saved = options[0];
  assert.equal(saved.id, 'grok-4-7');
  assert.equal(saved.saved, true);
  assert.equal(saved.known, false);
});

test('the nearest documented id is suggested for a guessed spelling', () => {
  assert.equal(closestMatch('xai', 'grok-4-7'), 'grok-4.7');
  assert.equal(closestMatch('xai', 'grok-4.20-non-reasoning'),
               'grok-4.20-0309-non-reasoning');
});

test('providers without a documented catalog report their raw id', () => {
  assert.equal(providerName('openai-codex'), 'openai-codex');
  assert.deepEqual(catalogFor('openai-codex'), []);
});
