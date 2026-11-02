// Window wiring: boot the engine, route frames, switch views.

import { connect, listenTo, onEvent, request } from './api.js';
import * as chat from './chat.js';
import * as settings from './settings.js';
import * as approvals from './approvals.js';

const statusDot = document.getElementById('status-dot');
const statusText = document.getElementById('status');
const modelChip = document.getElementById('model-chip');
const autonomyToggle = document.getElementById('autonomy-toggle');
const autonomyState = document.getElementById('autonomy-state');
const banner = document.getElementById('banner');
const bannerText = document.getElementById('banner-text');
const bannerAction = document.getElementById('banner-action');
const headerTitle = document.getElementById('header-title');
const headerSub = document.getElementById('header-sub');
const chatView = document.getElementById('chat-view');
const settingsView = document.getElementById('settings-view');

let snapshot = null;
let activeTab = 'chat';

function updateBanner() {
  if (snapshot && snapshot.setup_issue && activeTab === 'chat') {
    bannerText.textContent = snapshot.setup_issue;
    banner.hidden = false;
    return;
  }
  banner.hidden = true;
}

function setStatus(text, tone) {
  statusText.textContent = text;
  statusDot.className = 'dot';
  if (tone) {
    statusDot.classList.add(tone);
  }
}

function applySnapshot(next) {
  if (!next) {
    return;
  }
  snapshot = next;
  settings.applySnapshot(next);
  autonomyToggle.checked = Boolean(next.autonomy && next.autonomy.enabled);
  autonomyState.textContent = next.autonomy && next.autonomy.enabled ? 'On' : 'Off';
  modelChip.textContent = next.model || 'No model';
  modelChip.title = next.settings ? next.settings.model_base_url || '' : '';
  if (next.busy) {
    setStatus('Working', 'busy');
  } else if (next.setup_issue) {
    setStatus('Needs setup', 'error');
  } else {
    setStatus('Ready', 'ready');
  }
  if (next.setup_issue) {
    bannerText.textContent = next.setup_issue;
  }
  approvals.sync(next.pending_approvals);
  updateBanner();
}

function handleFrame(frame) {
  if (frame.event === 'stream') {
    chat.appendStream(frame.data.text || '');
    return;
  }
  if (frame.event === 'message') {
    chat.addRow(frame.data);
    if (frame.data.kind === 'agent' || frame.data.kind === 'thought') {
      chat.clearStream();
    }
    return;
  }
  if (frame.event === 'activity') {
    applyActivity(frame.data);
    return;
  }
  if (frame.event === 'approval') {
    approvals.show(frame.data);
    return;
  }
  if (frame.event === 'snapshot') {
    applySnapshot(frame.data);
    return;
  }
  if (frame.event === 'exit') {
    setStatus('Engine stopped', 'error');
  }
}

function applyActivity(data) {
  const phase = data.phase || '';
  if (phase === 'ready') {
    setStatus(snapshot && snapshot.setup_issue ? 'Needs setup' : 'Ready', 'ready');
    return;
  }
  if (phase === 'heartbeat') {
    return;
  }
  if (phase === 'model_start') {
    setStatus('Thinking', 'busy');
    return;
  }
  if (phase === 'tool') {
    setStatus(data.text ? 'Using ' + data.text : 'Using a tool', 'busy');
    return;
  }
  if (phase === 'compacting') {
    setStatus('Compacting history', 'busy');
    return;
  }
  if (phase === 'status' && data.text) {
    setStatus(data.text, 'busy');
  }
}

function openSettings() {
  activeTab = 'settings';
  for (const tab of document.querySelectorAll('.tab')) {
    tab.classList.toggle('active', tab.dataset.tab === 'settings');
  }
  chatView.hidden = true;
  settingsView.hidden = false;
  headerTitle.textContent = 'Settings';
  headerSub.textContent = 'Personality and model connection';
  updateBanner();
}

function wireTabs() {
  for (const tab of document.querySelectorAll('.tab')) {
    tab.addEventListener('click', () => {
      const name = tab.dataset.tab;
      activeTab = name;
      for (const other of document.querySelectorAll('.tab')) {
        other.classList.toggle('active', other === tab);
      }
      chatView.hidden = name !== 'chat';
      settingsView.hidden = name !== 'settings';
      headerTitle.textContent = name === 'chat' ? 'Chat' : 'Settings';
      headerSub.textContent =
        name === 'chat' ? 'One continuing conversation' : 'Personality and model connection';
      updateBanner();
    });
  }
}

function wireAutonomy() {
  autonomyToggle.addEventListener('change', async () => {
    if (!snapshot) {
      return;
    }
    const desired = autonomyToggle.checked;
    autonomyToggle.disabled = true;
    try {
      const next = await request('autonomy/set', {
        enabled: desired,
        expected_revision: snapshot.settings_revision,
      });
      applySnapshot(next);
    } catch (error) {
      autonomyToggle.checked = !desired;
      bannerText.textContent = error.message;
      banner.hidden = false;
    } finally {
      autonomyToggle.disabled = false;
    }
  });
}

async function boot() {
  try {
    await connect();
    onEvent(handleFrame);
    await listenTo('tauri://drag-drop', (event) => {
      const paths = (event.payload && event.payload.paths) || [];
      chat.addAttachments(paths);
    });
    chat.initChat();
    approvals.initApprovals();
    settings.initSettings({
      onSaved: (next) => {
        applySnapshot(next);
        chat.appendNote('Settings saved.');
      },
    });
    wireTabs();
    wireAutonomy();
    bannerAction.addEventListener('click', openSettings);
    const initial = await request('bootstrap');
    applySnapshot(initial);
    await chat.loadHistory();
  } catch (error) {
    setStatus(error.message, 'error');
    chat.appendNote(error.message);
  }
}

boot();
