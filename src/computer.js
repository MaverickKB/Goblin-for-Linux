// Computer use panel: inspection first, input still goes through approvals.

import { request } from './api.js';

const shotButton = document.getElementById('computer-shot');
const windowsButton = document.getElementById('computer-windows');
const status = document.getElementById('computer-status');
const output = document.getElementById('computer-output');
const typeInput = document.getElementById('computer-text');
const typeButton = document.getElementById('computer-type');
const keyInput = document.getElementById('computer-key');
const keyButton = document.getElementById('computer-key-run');
const windowSelect = document.getElementById('computer-window');
const focusButton = document.getElementById('computer-focus');
const scrollInput = document.getElementById('computer-dy');
const scrollButton = document.getElementById('computer-scroll');

async function run(name, arguments_) {
  status.textContent = 'Running';
  try {
    const record = await request('tool/execute', { name, arguments: arguments_ }, 60000);
    status.textContent = record.success ? 'Done' : (record.error || 'Failed');
    return record;
  } catch (error) {
    status.textContent = error.message;
    return null;
  }
}

async function screenshot() {
  output.textContent = '';
  const record = await run('computer_screenshot', {});
  if (!record || !record.image || !record.image.data_url) {
    return;
  }
  const image = document.createElement('img');
  image.className = 'computer-image';
  image.src = record.image.data_url;
  image.alt = record.image.caption || 'Host screen';
  image.addEventListener('click', async (event) => {
    const bounds = image.getBoundingClientRect();
    const x = Math.round(((event.clientX - bounds.left) / bounds.width) * image.naturalWidth);
    const y = Math.round(((event.clientY - bounds.top) / bounds.height) * image.naturalHeight);
    await run('computer_mouse', { action: 'click', x, y });
  });
  const caption = document.createElement('p');
  caption.className = 'muted';
  caption.textContent = record.image.caption || '';
  output.append(image, caption);
}

async function windows() {
  output.textContent = '';
  const record = await run('computer_windows', {});
  if (!record) {
    return;
  }
  const pre = document.createElement('pre');
  pre.className = 'computer-json';
  pre.textContent = record.result || '';
  output.append(pre);
  windowSelect.textContent = '';
  try {
    const parsed = JSON.parse(record.result || '{}');
    for (const item of parsed.windows || []) {
      const option = document.createElement('option');
      option.value = item.id || item.title || '';
      option.textContent = item.title || item.app || item.id || 'Window';
      windowSelect.append(option);
    }
  } catch (error) {
    // The raw JSON stays visible above.
  }
}

export function initComputer() {
  shotButton.addEventListener('click', screenshot);
  windowsButton.addEventListener('click', windows);
  typeButton.addEventListener('click', async () => {
    if (typeInput.value) {
      await run('computer_type', { text: typeInput.value });
    }
  });
  keyButton.addEventListener('click', async () => {
    if (keyInput.value) {
      await run('computer_key', { keys: keyInput.value });
    }
  });
  focusButton.addEventListener('click', async () => {
    if (windowSelect.value) {
      await run('computer_focus', { target: windowSelect.value });
    }
  });
  scrollButton.addEventListener('click', async () => {
    await run('computer_scroll', { dy: Number(scrollInput.value) || 0 });
  });
}
