// Window wiring: one transparent companion window, one engine.

import { connect, listenTo, notify, onEvent, request } from './api.js';
import * as chat from './chat.js';
import * as settings from './settings.js';
import * as approvals from './approvals.js';
import * as connections from './connections.js';
import * as floor from './floor.js';
import * as tools from './tools.js';
import * as computer from './computer.js';
import * as voice from './voice.js';
import { initCharacter, setCharacterSignals } from './character.js';

const app = document.getElementById('app');
const board = document.getElementById('board');
const characterCanvas = document.getElementById('character');
const speech = document.getElementById('speech');
const speechText = document.getElementById('speech-text');
const speechBubble = document.getElementById('speech-bubble');
const agentNameLabel = document.getElementById('agent-name-label');
const modelName = document.getElementById('model-name');
const turnCount = document.getElementById('turn-count');
const autonomyPill = document.getElementById('autonomy-pill');
const autonomyLabel = document.getElementById('autonomy-label');
const settingsOpen = document.getElementById('settings-open');
const settingsBack = document.getElementById('settings-back');
const settingsView = document.getElementById('settings-view');
const chatView = document.getElementById('chat-view');
const collapseButton = document.getElementById('collapse');
const liveStrip = document.getElementById('live-strip');
const livePhase = document.getElementById('live-phase');
const liveDetail = document.getElementById('live-detail');
const composer = document.getElementById('composer');
const input = document.getElementById('input');
const sendButton = document.getElementById('send');
const stopButton = document.getElementById('stop');
const composerHint = document.getElementById('composer-hint');
const bootError = document.getElementById('boot-error');

let snapshot = null;
let collapsed = false;
let busy = false;
let completionTimer = 0;

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

function updateSpeech() {
  if (!collapsed) {
    speech.hidden = true;
    return;
  }
  let text = chat.lastReply();
  if (characterSignals.approval) {
    text = 'I need your approval.';
  } else if (busy) {
    text = livePhase.textContent || 'Working';
  }
  if (!text) {
    text = 'Open the chat.';
  }
  speechText.textContent = text;
  speech.hidden = false;
}

function updateCharacter(partial) {
  characterSignals = { ...characterSignals, ...partial };
  setCharacterSignals(partial);
  updateSpeech();
}

function updateMeta() {
  const count = chat.rowCount();
  if (snapshot && snapshot.model) {
    modelName.textContent = snapshot.model;
  }
  if (snapshot && snapshot.settings && snapshot.settings.agent_name) {
    agentNameLabel.textContent = snapshot.settings.agent_name;
  }
  turnCount.textContent = count + ' turns, showing ' + count;
}

function setLive(text, detail) {
  liveStrip.hidden = false;
  livePhase.textContent = text;
  liveDetail.textContent = detail || '';
  updateSpeech();
}

function clearLive() {
  liveStrip.hidden = true;
  livePhase.textContent = '';
  liveDetail.textContent = '';
}

function setBusy(next) {
  busy = next;
  stopButton.hidden = !next;
  sendButton.disabled = false;
  composerHint.textContent = next ? 'Return steers the running turn.' : 'Return sends.';
  if (!next) {
    clearLive();
  }
  updateSpeech();
}

function showBootError(message) {
  bootError.hidden = false;
  bootError.textContent = message;
}

function applySnapshot(next) {
  if (!next) {
    return;
  }
  snapshot = next;
  settings.applySnapshot(next);
  const autonomous = Boolean(next.autonomy && next.autonomy.enabled);
  autonomyLabel.textContent = autonomous ? 'Autonomy on' : 'Autonomy off';
  autonomyPill.classList.toggle('on', autonomous);
  approvals.sync(next.pending_approvals);
  floor.renderContinuity(next.memory);
  tools.renderTools(next);
  updateCharacter({
    approval: (next.pending_approvals || []).length > 0,
    autonomy: autonomous,
  });
  if (next.model) {
    modelName.textContent = next.model;
  }
  if (next.settings && next.settings.agent_name) {
    agentNameLabel.textContent = next.settings.agent_name;
  }
  if (next.busy) {
    setBusy(true);
    setLive(next.activity || 'Working');
  }
  updateMeta();
}

function handleFrame(frame) {
  if (frame.event === 'stream') {
    chat.appendStream(frame.data.text || '');
    updateCharacter({ speaking: true, thinking: false, speechLevel: 0.5 });
    setLive('Speaking');
    return;
  }
  if (frame.event === 'message') {
    chat.addRow(frame.data);
    if (frame.data.kind === 'agent') {
      chat.clearStream();
      updateCharacter({ speaking: false, thinking: false, working: false, speechLevel: 0, completed: true });
      setBusy(false);
      if (!document.hasFocus() && frame.data.text) {
        notify('Goblin replied', String(frame.data.text).slice(0, 140));
      }
      clearTimeout(completionTimer);
      completionTimer = setTimeout(() => updateCharacter({ completed: false }), 1400);
    } else if (frame.data.kind === 'thought') {
      chat.clearStream();
      updateCharacter({ thinking: true, speaking: false });
    }
    updateMeta();
    return;
  }
  if (frame.event === 'activity') {
    applyActivity(frame.data);
    return;
  }
  if (frame.event === 'frame_error') {
    showBootError(frame.data.message);
    return;
  }
  if (frame.event === 'approval') {
    approvals.show(frame.data);
    updateCharacter({ approval: true });
    notify('Goblin needs approval', (frame.data && frame.data.name) || 'A tool is waiting');
    return;
  }
  if (frame.event === 'snapshot') {
    applySnapshot(frame.data);
    return;
  }
  if (frame.event === 'exit') {
    showBootError('The local engine stopped.');
    setBusy(false);
    updateCharacter({ error: true, thinking: false, working: false, speaking: false });
  }
}

function applyActivity(data) {
  const phase = data.phase || '';
  if (phase === 'ready') {
    setLive('Ready');
    updateCharacter({ working: false, thinking: false });
    return;
  }
  if (phase === 'heartbeat') {
    return;
  }
  if (phase === 'model_start') {
    setLive('Thinking');
    updateCharacter({ thinking: true, working: false });
    return;
  }
  if (phase === 'tool') {
    const toolName = String(data.text || '').trim().split(/\s+/)[0] || 'tool';
    setLive('Working', data.text || '');
    updateCharacter({
      working: true,
      thinking: false,
      toolKind: toolName,
      activityToken: characterSignals.activityToken + 1,
    });
    return;
  }
  if (phase === 'compacting') {
    setLive('Compacting history');
    updateCharacter({ working: true });
    return;
  }
  if (phase === 'status' && data.text) {
    setLive(data.text);
  }
}

function openSettings() {
  chatView.hidden = true;
  settingsView.hidden = false;
  if (snapshot) {
    floor.loadFloor(snapshot);
  }
}

function closeSettings() {
  settingsView.hidden = true;
  chatView.hidden = false;
}

async function setCollapsed(next) {
  collapsed = next;
  app.classList.toggle('collapsed', next);
  if (next) {
    closeSettings();
  }
  updateSpeech();
  try {
    await window.__TAURI__.core.invoke('set_window_mode', { collapsed: next });
  } catch (error) {
    showBootError(error.message);
  }
}

function wireAutonomy() {
  autonomyPill.addEventListener('click', async () => {
    if (!snapshot) {
      return;
    }
    const desired = !(snapshot.autonomy && snapshot.autonomy.enabled);
    autonomyPill.disabled = true;
    try {
      const next = await request('autonomy/set', {
        enabled: desired,
        expected_revision: snapshot.settings_revision,
      });
      applySnapshot(next);
    } catch (error) {
      showBootError(error.message);
    } finally {
      autonomyPill.disabled = false;
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
      onSaved: (next) => applySnapshot(next),
    });
    connections.initConnections();
    floor.initFloor();
    tools.initTools();
    computer.initComputer();
    voice.initVoice();
    wireAutonomy();

    settingsOpen.addEventListener('click', openSettings);
    settingsBack.addEventListener('click', closeSettings);
    collapseButton.addEventListener('click', () => setCollapsed(!collapsed));
    speechBubble.addEventListener('click', () => setCollapsed(false));

    input.addEventListener('focus', () => updateCharacter({ listening: true }));
    input.addEventListener('blur', () => updateCharacter({ listening: false, typing: false }));
    input.addEventListener('input', () => updateCharacter({ typing: input.value.trim().length > 0 }));
    composer.addEventListener('submit', () => {
      updateCharacter({ typing: false, thinking: true });
      setBusy(true);
      setLive('Thinking');
    });
    stopButton.addEventListener('click', async () => {
      try {
        await request('chat/stop', {});
      } catch (error) {
        showBootError(error.message);
      }
    });
    window.addEventListener('goblin:send-failed', (event) => {
      setBusy(false);
      showBootError(event.detail || 'The message could not be sent.');
    });

    initCharacter(characterCanvas, { avatar: 'rookie' }).catch((error) => {
      chat.appendNote('Character: ' + error.message);
    });

    const initial = await request('bootstrap');
    applySnapshot(initial);
    floor.loadFloor(initial);
    await chat.loadHistory();
    updateMeta();
    updateSpeech();
  } catch (error) {
    showBootError(error.message);
    chat.appendNote(error.message);
  }
}

boot();
