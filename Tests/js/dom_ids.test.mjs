// Run with: node --test Tests/js
// The modules query the markup by id. A missing id is a silent dead control,
// so this fails when a script names an element the page does not have.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

const SRC = new URL('../../src/', import.meta.url).pathname;

function markupIds() {
  const html = readFileSync(join(SRC, 'index.html'), 'utf8');
  const ids = new Set();
  for (const match of html.matchAll(/\sid="([^"]+)"/g)) {
    ids.add(match[1]);
  }
  return ids;
}

function queriedIds(file) {
  const source = readFileSync(join(SRC, file), 'utf8');
  return [...source.matchAll(/getElementById\('([^']+)'\)/g)].map((match) => match[1]);
}

test('every element a module queries exists in the markup', () => {
  const ids = markupIds();
  const missing = [];
  for (const file of readdirSync(SRC).filter((name) => name.endsWith('.js'))) {
    for (const id of queriedIds(file)) {
      if (!ids.has(id)) {
        missing.push(file + ' -> #' + id);
      }
    }
  }
  assert.deepEqual(missing, []);
});

test('the model and compression controls are real selects', () => {
  const html = readFileSync(join(SRC, 'index.html'), 'utf8');
  for (const id of ['model', 'model-connection', 'compactor-connection', 'compactor-model']) {
    const tag = html.match(new RegExp('<select id="' + id + '"[^>]*>'));
    assert.ok(tag, '#' + id + ' must be a select');
  }
  assert.equal(/<input id="model"/.test(html), false,
               'the chat model must not be a free-text input');
});
