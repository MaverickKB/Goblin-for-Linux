// Transcript rendering and chat intake.

import { request } from './api.js';

const transcript = document.getElementById('transcript');
const composer = document.getElementById('composer');
const input = document.getElementById('input');
const sendButton = document.getElementById('send');
const attachButton = document.getElementById('attach');
const attachmentStrip = document.getElementById('attachments');

const rows = new Map();
let liveRow = null;
let pendingLocalId = null;
let pendingLocalText = '';
let attached = [];
let lastAgentText = '';

export function rowCount() {
  return rows.size;
}

export function lastReply() {
  return lastAgentText;
}

function formatTime(value) {
  if (!value) {
    return '';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return '';
  }
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function buildRow(row) {
  const article = document.createElement('article');
  article.className = 'row row-' + row.kind;
  article.dataset.id = row.id;

  const meta = document.createElement('div');
  meta.className = 'meta';
  const name = document.createElement('span');
  name.className = 'name';
  name.textContent = row.name || labelFor(row.kind);
  const time = document.createElement('span');
  time.textContent = formatTime(row.timestamp);
  meta.append(name, time);
  if (row.status) {
    const status = document.createElement('span');
    status.className = 'status';
    status.textContent = row.status;
    meta.append(status);
  }

  const text = document.createElement('div');
  text.className = 'text';
  text.textContent = row.text || '';

  article.append(meta, text);
  if (row.attachments && row.attachments.length > 0) {
    const files = document.createElement('div');
    files.className = 'files';
    for (const path of row.attachments) {
      const name = document.createElement('span');
      name.textContent = fileName(path);
      files.append(name);
    }
    article.append(files);
  }
  return article;
}

function fileName(path) {
  const parts = String(path).split('/');
  return parts[parts.length - 1] || String(path);
}

export function addAttachments(paths) {
  if (!Array.isArray(paths)) {
    return;
  }
  const fresh = [];
  for (const path of paths) {
    if (typeof path !== 'string' || !path.startsWith('/')) {
      continue;
    }
    if (attached.includes(path)) {
      continue;
    }
    fresh.push(path);
  }
  attached = attached.concat(fresh).slice(0, 6);
  renderAttachments();
}

function renderAttachments() {
  attachmentStrip.textContent = '';
  attachmentStrip.hidden = attached.length === 0;
  for (const path of attached) {
    const chip = document.createElement('span');
    chip.className = 'attachment';
    const label = document.createElement('span');
    label.textContent = fileName(path);
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = 'x';
    remove.title = 'Remove attachment';
    remove.addEventListener('click', () => {
      attached = attached.filter((item) => item !== path);
      renderAttachments();
    });
    chip.append(label, remove);
    attachmentStrip.append(chip);
  }
}

async function openPicker() {
  try {
    const result = await window.__TAURI__.core.invoke('plugin:dialog|open', {
      options: { multiple: true, directory: false, title: 'Attach files' },
    });
    if (!result) {
      return;
    }
    addAttachments(Array.isArray(result) ? result : [result]);
  } catch (error) {
    appendNote(error.message);
  }
}

function labelFor(kind) {
  if (kind === 'user') {
    return 'You';
  }
  if (kind === 'thought') {
    return 'Thinking';
  }
  if (kind === 'tool') {
    return 'Tool';
  }
  if (kind === 'result') {
    return 'Result';
  }
  if (kind === 'note') {
    return 'Note';
  }
  return 'Goblin';
}

function atBottom() {
  return transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 120;
}

function scrollToEnd(force) {
  if (force || atBottom()) {
    transcript.scrollTop = transcript.scrollHeight;
  }
}

function showEmptyState() {
  return;
}

export function addRow(row) {
  if (!row || !row.id || rows.has(row.id)) {
    return;
  }
  if (row.kind === 'user' && pendingLocalId && row.text === pendingLocalText) {
    removeRow(pendingLocalId);
  }
  const stick = atBottom();
  const article = buildRow(row);
  rows.set(row.id, article);
  transcript.append(article);
  if (row.kind === 'agent' && row.text) {
    lastAgentText = row.text;
  }
  showEmptyState();
  scrollToEnd(stick);
}

function removeRow(id) {
  const article = rows.get(id);
  if (article) {
    article.remove();
    rows.delete(id);
  }
}

export function updateRow(row) {
  const article = rows.get(row.id);
  if (!article) {
    addRow(row);
    return;
  }
  const text = article.querySelector('.text');
  if (row.text && text.textContent !== row.text) {
    text.textContent = row.text;
  }
}

function startLiveRow() {
  if (liveRow) {
    return liveRow;
  }
  liveRow = document.createElement('article');
  liveRow.className = 'row row-agent live';
  const meta = document.createElement('div');
  meta.className = 'meta';
  const name = document.createElement('span');
  name.className = 'name';
  name.textContent = 'Goblin';
  meta.append(name);
  const text = document.createElement('div');
  text.className = 'text';
  liveRow.append(meta, text);
  transcript.append(liveRow);
  showEmptyState();
  return liveRow;
}

export function appendStream(text) {
  const stick = atBottom();
  const article = startLiveRow();
  const body = article.querySelector('.text');
  body.textContent += text;
  scrollToEnd(stick);
}

export function clearStream() {
  if (liveRow) {
    liveRow.remove();
    liveRow = null;
  }
}

export function resetTranscript() {
  clearStream();
  for (const article of rows.values()) {
    article.remove();
  }
  rows.clear();
  pendingLocalId = null;
  pendingLocalText = '';
  showEmptyState();
}

export async function loadHistory() {
  const page = await request('history/page', { limit: 120 });
  resetTranscript();
  for (const row of page.messages || []) {
    addRow(row);
  }
  scrollToEnd(true);
  return page;
}

async function send(text) {
  const trimmed = text.trim();
  if (!trimmed && attached.length === 0) {
    return;
  }
  pendingLocalId = 'local-' + Date.now();
  pendingLocalText = trimmed;
  addRow({
    id: pendingLocalId,
    kind: 'user',
    name: 'You',
    text: trimmed || 'Attached files',
    timestamp: new Date().toISOString(),
    attachments: attached.slice(),
  });
  input.value = '';
  resizeInput();
  sendButton.disabled = true;
  try {
    await request('chat/send', {
      text: trimmed || 'See the attached files.',
      attachments: attached.slice(),
    });
    attached = [];
    renderAttachments();
    clearStream();
  } catch (error) {
    const article = rows.get(pendingLocalId);
    if (article) {
      const meta = article.querySelector('.meta');
      const status = document.createElement('span');
      status.className = 'status';
      status.textContent = 'failed';
      meta.append(status);
    }
    appendNote(error.message);
    window.dispatchEvent(new CustomEvent('goblin:send-failed', { detail: error.message }));
  } finally {
    sendButton.disabled = false;
    input.focus();
  }
}

export function appendNote(text) {
  addRow({
    id: 'note-' + Date.now(),
    kind: 'note',
    name: 'Goblin',
    text,
    timestamp: new Date().toISOString(),
  });
}

function resizeInput() {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 220) + 'px';
}

export function initChat() {
  attachButton.addEventListener('click', openPicker);
  composer.addEventListener('submit', (event) => {
    event.preventDefault();
    send(input.value);
  });
  input.addEventListener('input', resizeInput);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      composer.requestSubmit();
    }
  });
  showEmptyState();
}
