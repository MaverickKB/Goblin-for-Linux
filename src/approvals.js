// Tool approvals. The engine blocks the tool call until this resolves it.

import { request } from './api.js';

const panel = document.getElementById('approval');
const title = document.getElementById('approval-title');
const detail = document.getElementById('approval-detail');
const allow = document.getElementById('approval-allow');
const deny = document.getElementById('approval-deny');

let current = null;

function describe(requestInfo) {
  const name = requestInfo.name || 'a tool';
  return 'Goblin wants to use ' + name;
}

export function show(requestInfo) {
  if (!requestInfo || !requestInfo.id) {
    return;
  }
  current = requestInfo;
  title.textContent = describe(requestInfo);
  try {
    detail.textContent = JSON.stringify(requestInfo.arguments || {}, null, 2);
  } catch (error) {
    detail.textContent = String(requestInfo.arguments);
  }
  panel.hidden = false;
}

export function clear() {
  current = null;
  panel.hidden = true;
}

export function sync(pending) {
  if (!pending || pending.length === 0) {
    clear();
    return;
  }
  show(pending[0]);
}

async function resolve(accepted) {
  if (!current) {
    return;
  }
  const id = current.id;
  allow.disabled = true;
  deny.disabled = true;
  try {
    await request('approval/resolve', { id, accepted });
    clear();
  } catch (error) {
    detail.textContent = error.message;
  } finally {
    allow.disabled = false;
    deny.disabled = false;
  }
}

export function initApprovals() {
  allow.addEventListener('click', () => resolve(true));
  deny.addEventListener('click', () => resolve(false));
  panel.hidden = true;
}
