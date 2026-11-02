// Window wiring: boot the engine, route frames, switch views.

import { connect, listenTo, onEvent, request } from './api.js';
import * as chat from './chat.js';
import * as settings from './settings.js';
import * as approvals from './approvals.js';
import * as connections from './connections.js';
import { initCharacter, setCharacterSignals } from './character.js';

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
const characterCanvas = document.getElementById('character');
const companionCaption = document.getElementById('companion-caption');

let characterSignals = {
  listening: false,
  thinking: false,
  working: false,
  speaking: false,
  attention: true,
  speechLevel: 0,
  error: false,
  approval: false,
  autonomy: false,
  typing: false,
  toolKind: '',
  activityToken: 0,
  completed: false,
};

let characterStage = 'starting';

function updateCompanionCaption() {
  let text = 'Idle';
  if (characterSignals.error) {
    text = 'Concerned';
  } else if (characterSignals.approval) {
    text = 'Waiting for you';
  } else if (characterSignals.working) {
    text = 'Working';
  } else if (characterSignals.thinking) {
    text = 'Thinking';
  } else if (characterSignals.speaking) {
    text = 'Speaking';
  } else if (characterSignals.typing) {
    text = 'Listening';
  }
  companionCaption.textContent = characterStage + ' / ' + text;
}

function updateCharacter(partial) {
  characterSignals = { ...characterSignals, ...partial };
  setCharacterSignals(partial);
  updateCompanionCaption();
}

let snapshot = null;
let activeTab = 'chat';
let completionTimer = 0;

function showStaticCharacter() {
  characterCanvas.hidden = true;
  const image = document.createElement('img');
  image.src = 'assets/avatar.png';
  image.alt = '';
  characterCanvas.parentElement.append(image);
  companionCaption.textContent = 'Static';
}

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
  updateCharacter({
    approval: (next.pending_approvals || []).length > 0,
    autonomy: Boolean(next.autonomy && next.autonomy.enabled),
  });
  updateBanner();
}

function handleFrame(frame) {
  if (frame.event === 'stream') {
    chat.appendStream(frame.data.text || '');
    updateCharacter({ speaking: true, thinking: false, speechLevel: 0.5 });
    return;
  }
  if (frame.event === 'message') {
    chat.addRow(frame.data);
    if (frame.data.kind === 'agent') {
      chat.clearStream();
      updateCharacter({ speaking: false, thinking: false, working: false, speechLevel: 0, completed: true });
      clearTimeout(completionTimer);
      completionTimer = setTimeout(() => updateCharacter({ completed: false }), 1400);
    } else if (frame.data.kind === 'thought') {
      chat.clearStream();
      updateCharacter({ thinking: true, speaking: false });
    }
    return;
  }
  if (frame.event === 'activity') {
    applyActivity(frame.data);
    return;
  }
  if (frame.event === 'frame_error') {
    setStatus(frame.data.message, 'error');
    return;
  }
  if (frame.event === 'approval') {
    approvals.show(frame.data);
    updateCharacter({ approval: true });
    return;
  }
  if (frame.event === 'snapshot') {
    applySnapshot(frame.data);
    return;
  }
  if (frame.event === 'exit') {
    setStatus('Engine stopped', 'error');
    updateCharacter({ error: true, thinking: false, working: false, speaking: false });
  }
}

function applyActivity(data) {
  const phase = data.phase || '';
  if (phase === 'ready') {
    setStatus(snapshot && snapshot.setup_issue ? 'Needs setup' : 'Ready', 'ready');
    updateCharacter({ working: false, thinking: false });
    return;
  }
  if (phase === 'heartbeat') {
    return;
  }
  if (phase === 'model_start') {
    setStatus('Thinking', 'busy');
    updateCharacter({ thinking: true, working: false });
    return;
  }
  if (phase === 'tool') {
    const toolName = String(data.text || '').trim().split(/\s+/)[0] || 'tool';
    setStatus(data.text ? 'Using ' + data.text : 'Using a tool', 'busy');
    updateCharacter({
      working: true,
      thinking: false,
      toolKind: toolName,
      activityToken: characterSignals.activityToken + 1,
    });
    return;
  }
  if (phase === 'compacting') {
    setStatus('Compacting history', 'busy');
    updateCharacter({ working: true });
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
    setStatus('Connecting', 'busy');
    window.__TAURI__.event.listen('ui-probe', (event) => {
      setStatus('Probe ' + event.payload, 'busy');
    });
    await connect();
    setStatus('Loading', 'busy');
    onEvent(handleFrame);
    await listenTo('tauri://drag-drop', (event) => {
      const paths = (event.payload && event.payload.paths) || [];
      chat.addAttachments(paths);
    });
    chat.initChat();
    approvals.initApprovals();
    const input = document.getElementById('input');
    input.addEventListener('focus', () => updateCharacter({ listening: true }));
    input.addEventListener('blur', () => updateCharacter({ listening: false, typing: false }));
    input.addEventListener('input', () => updateCharacter({ typing: input.value.trim().length > 0 }));
    document.getElementById('composer').addEventListener('submit', () => {
      updateCharacter({ typing: false, thinking: true });
    });
    settings.initSettings({
      onSaved: (next) => {
        applySnapshot(next);
        chat.appendNote('Settings saved.');
      },
    });
    connections.initConnections();
    wireTabs();
    wireAutonomy();
    bannerAction.addEventListener('click', openSettings);
    const initial = await request('bootstrap');
    modelChip.textContent = 'M:' + (initial && initial.model ? initial.model : 'none');
    characterStage = 'called';
    updateCompanionCaption();
    initCharacter(characterCanvas, { avatar: 'rookie' })
      .then(() => {
        characterStage = 'live';
        updateCompanionCaption();
      })
      .catch((error) => {
        characterStage = 'failed';
        updateCompanionCaption();
        chat.appendNote('Character: ' + error.message);
        showStaticCharacter();
      });
    applySnapshot(initial);
    await chat.loadHistory();
  } catch (error) {
    setStatus(error.message, 'error');
    chat.appendNote(error.message);
  }
}

boot();
