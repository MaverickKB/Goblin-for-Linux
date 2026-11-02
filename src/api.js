// The only path between the web UI and the Python engine.

const tauri = window.__TAURI__;
const pending = new Map();
const handlers = [];
let nextId = 1;

export function request(method, params = {}, timeoutMs = 30000) {
  if (!tauri) {
    return Promise.reject(new Error('This page must run inside the Goblin app.'));
  }
  const id = 'ui-' + nextId++;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(method + ' timed out'));
    }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    const frame = JSON.stringify({ id, method, params });
    tauri.core.invoke('engine_request', { frame }).catch((error) => {
      clearTimeout(timer);
      pending.delete(id);
      reject(error);
    });
  });
}

export function onEvent(handler) {
  handlers.push(handler);
}

export function listenTo(event, handler) {
  if (!tauri) {
    return Promise.resolve();
  }
  return tauri.event.listen(event, handler);
}

export async function notify(title, body) {
  if (!tauri) {
    return;
  }
  try {
    await tauri.core.invoke('notify', { title, body });
  } catch (error) {
    // Notifications are best effort; a missing desktop bus is not fatal.
  }
}

function deliver(frame) {
  for (const handler of handlers) {
    handler(frame);
  }
}

function receive(payload) {
  let frame;
  try {
    frame = typeof payload === 'string' ? JSON.parse(payload) : payload;
  } catch (error) {
    deliver({ event: 'frame_error', data: { message: error.message } });
    return;
  }
  if (!frame || typeof frame !== 'object') {
    deliver({ event: 'frame_error', data: { message: 'The engine sent a frame that was not an object.' } });
    return;
  }
  if (frame.event) {
    deliver(frame);
    return;
  }
  const entry = pending.get(frame.id);
  if (!entry) {
    return;
  }
  clearTimeout(entry.timer);
  pending.delete(frame.id);
  if (frame.error) {
    entry.reject(new Error(frame.error.message || 'The engine reported an error.'));
  } else {
    entry.resolve(frame.result);
  }
}

export async function connect() {
  if (!tauri) {
    throw new Error('This page must run inside the Goblin app.');
  }
  let polling = true;
  const poll = async () => {
    while (polling) {
      let frames;
      try {
        frames = await tauri.core.invoke('engine_poll');
      } catch (error) {
        polling = false;
        for (const entry of pending.values()) {
          clearTimeout(entry.timer);
          entry.reject(new Error('The engine stopped.'));
        }
        pending.clear();
        deliver({ event: 'exit', data: {} });
        return;
      }
      for (const payload of frames) {
        receive(payload);
      }
      await new Promise((resolve) => setTimeout(resolve, 40));
    }
  };
  poll();
}
