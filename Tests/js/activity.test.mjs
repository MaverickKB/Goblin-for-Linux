// Run with: node --test Tests/js

import test from 'node:test';
import assert from 'node:assert/strict';

import { describeActivity } from '../../src/activity.js';

test('the compacting phase says nothing in chat', () => {
  const shown = describeActivity('compacting', {
    text: '16000 bytes, 3 pending summaries',
    compaction: { bytes: 16000, pending: 3 },
  });
  assert.equal(shown.live, null);
  assert.deepEqual(shown.character, {});
});

test('a tool phase still reports what is running', () => {
  const shown = describeActivity('tool', { text: 'read_file src/main.js' });
  assert.equal(shown.live.text, 'Working');
  assert.equal(shown.live.detail, 'read_file src/main.js');
  assert.equal(shown.character.toolKind, 'read_file');
});

test('model_start and ready keep their visible states', () => {
  assert.equal(describeActivity('model_start', {}).live.text, 'Thinking');
  assert.equal(describeActivity('ready', {}).live.text, 'Ready');
});

test('an unknown phase is silent instead of inventing a status', () => {
  assert.equal(describeActivity('something_new', { text: 'hi' }).live, null);
});
