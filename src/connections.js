// Account connections. Sign-in is a device code the engine owns; this panel
// only displays the code, opens the page, and polls the state.

import { request } from './api.js';

const list = document.getElementById('accounts');
let pollTimer = null;

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function accountRow(report) {
  const row = document.createElement('div');
  row.className = 'account';

  const copy = document.createElement('div');
  copy.className = 'account-copy';
  const name = document.createElement('strong');
  name.textContent = report.name || report.provider || 'Account';
  const message = document.createElement('small');
  message.textContent = report.message || report.state || '';
  copy.append(name, message);

  if (report.verification_url && report.user_code) {
    const code = document.createElement('div');
    code.className = 'device-code';
    const value = document.createElement('code');
    value.textContent = report.user_code;
    const open = document.createElement('button');
    open.type = 'button';
    open.textContent = 'Open sign-in page';
    open.addEventListener('click', () => {
      window.__TAURI__.core.invoke('open_url', { url: report.verification_url });
    });
    code.append(value, open);
    copy.append(code);
  }

  const actions = document.createElement('div');
  actions.className = 'account-actions';
  if (report.state === 'waiting') {
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.textContent = 'Cancel';
    cancel.addEventListener('click', async () => {
      await request('account/cancel', { provider: report.provider });
      load();
    });
    actions.append(cancel);
  } else if (report.state === 'signed_in') {
    const check = document.createElement('button');
    check.type = 'button';
    check.textContent = 'Check models';
    check.addEventListener('click', async () => {
      check.disabled = true;
      try {
        const result = await request('account/check', { provider: report.provider }, 45000);
        message.textContent = result.ok
          ? 'Connected. ' + (result.models ? result.models.length : 0) + ' models available.'
          : result.message || 'The check failed.';
      } catch (error) {
        message.textContent = error.message;
      } finally {
        check.disabled = false;
      }
    });
    actions.append(check);
  } else {
    const signIn = document.createElement('button');
    signIn.type = 'button';
    signIn.className = 'primary';
    signIn.textContent = 'Sign in';
    signIn.addEventListener('click', async () => {
      signIn.disabled = true;
      try {
        const reportNow = await request('account/start', { provider: report.provider }, 60000);
        render([reportNow, ...current.filter((item) => item.provider !== reportNow.provider)]);
        startPolling();
      } catch (error) {
        message.textContent = error.message;
      } finally {
        signIn.disabled = false;
      }
    });
    actions.append(signIn);
  }

  row.append(copy, actions);
  return row;
}

let current = [];

function render(accounts) {
  current = accounts || [];
  list.textContent = '';
  if (current.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'muted';
    empty.textContent = 'No account providers are configured.';
    list.append(empty);
    return;
  }
  for (const report of current) {
    list.append(accountRow(report));
  }
}

function waitingExists() {
  return current.some((report) => report.state === 'waiting');
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(async () => {
    try {
      await load();
    } catch (error) {
      stopPolling();
      return;
    }
    if (!waitingExists()) {
      stopPolling();
    }
  }, 3000);
}

export async function load() {
  const result = await request('account/list');
  render(result.accounts);
  if (waitingExists()) {
    startPolling();
  }
}

export function initConnections() {
  load().catch(() => {
    const note = document.createElement('p');
    note.className = 'muted';
    note.textContent = 'Connections are unavailable.';
    list.textContent = '';
    list.append(note);
  });
}
