// Computer use panel: inspection first, input still goes through approvals.

import { request } from './api.js';

const shotButton = document.getElementById('computer-shot');
const windowsButton = document.getElementById('computer-windows');
const status = document.getElementById('computer-status');
const output = document.getElementById('computer-output');

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
}

export function initComputer() {
  shotButton.addEventListener('click', screenshot);
  windowsButton.addEventListener('click', windows);
}
