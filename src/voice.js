// Voice session: engine lease, websocket, microphone and playback.

import { request } from './api.js';

const button = document.getElementById('voice');
const input = document.getElementById('input');

let lease = null;
let socket = null;
let capture = null;
let playback = null;
let stream = null;
let running = false;

function base64FromBytes(bytes) {
  let binary = '';
  const chunk = 0x8000;
  for (let index = 0; index < bytes.length; index += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(index, index + chunk));
  }
  return btoa(binary);
}

function bytesFromBase64(value) {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index++) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

function floatToPcm16(samples) {
  const bytes = new Uint8Array(samples.length * 2);
  const view = new DataView(bytes.buffer);
  for (let index = 0; index < samples.length; index++) {
    const sample = Math.max(-1, Math.min(1, samples[index]));
    view.setInt16(index * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
  }
  return bytes;
}

function pcm16ToFloat(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const samples = new Float32Array(bytes.length / 2);
  for (let index = 0; index < samples.length; index++) {
    samples[index] = view.getInt16(index * 2, true) / 0x8000;
  }
  return samples;
}

function setButton(active) {
  button.textContent = active ? 'End voice' : 'Voice';
}

function fail(message) {
  const box = document.getElementById('boot-error');
  if (box) {
    box.hidden = false;
    box.textContent = message;
  }
}

async function forward(events) {
  if (!lease) {
    return;
  }
  try {
    const result = await request('voice/events', { lease_id: lease.voice_id, events }, 30000);
    if (socket && socket.readyState === WebSocket.OPEN) {
      for (const event of result.send || []) {
        socket.send(JSON.stringify(event));
      }
    }
  } catch (error) {
    fail('Voice event failed: ' + error.message);
  }
}

function playbackChunk(bytes) {
  if (!playback) {
    return;
  }
  const samples = pcm16ToFloat(bytes);
  const buffer = playback.createBuffer(1, samples.length, lease.audio_sample_rate || 24000);
  buffer.copyToChannel(samples, 0);
  const source = playback.createBufferSource();
  source.buffer = buffer;
  source.connect(playback.destination);
  source.start();
}

function handleServerEvent(event) {
  const kind = event.type || '';
  if (kind.endsWith('audio.delta') && typeof event.delta === 'string') {
    playbackChunk(bytesFromBase64(event.delta));
    return;
  }
  const forwarded = [
    'input_audio_buffer.committed',
    'conversation.item.input_audio_transcription.completed',
    'response.audio_transcript.done',
    'response.output_audio_transcript.done',
    'response.function_call_arguments.done',
    'response.done',
  ];
  if (forwarded.includes(kind)) {
    forward([event]);
  }
}

function startCapture(context) {
  const source = context.createMediaStreamSource(stream);
  const processor = context.createScriptProcessor(2400, 1, 1);
  processor.onaudioprocess = (event) => {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return;
    }
    const samples = event.inputBuffer.getChannelData(0);
    socket.send(JSON.stringify({
      type: 'input_audio_buffer.append',
      audio: base64FromBytes(floatToPcm16(samples)),
    }));
  };
  const silence = context.createGain();
  silence.gain.value = 0;
  source.connect(processor);
  processor.connect(silence);
  silence.connect(context.destination);
  return { processor, silence };
}

async function start() {
  button.disabled = true;
  try {
    lease = await request('voice/start', {}, 60000);
    const protocols = lease.protocols || [];
    socket = new WebSocket(lease.websocket_url, protocols);
    socket.onopen = () => {
      if (lease.session_update) {
        socket.send(JSON.stringify(lease.session_update));
      }
    };
    socket.onmessage = (message) => {
      let event;
      try {
        event = JSON.parse(message.data);
      } catch (error) {
        return;
      }
      handleServerEvent(event);
    };
    socket.onerror = () => fail('The voice connection failed.');
    socket.onclose = () => stop(true);

    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
    playback = new AudioContext({ sampleRate: lease.audio_sample_rate || 24000 });
    capture = startCapture(playback);
    running = true;
    setButton(true);
  } catch (error) {
    fail('Voice: ' + error.message + ' Sign in with xAI in Connections and use a machine with a microphone.');
    await stop(false);
  } finally {
    button.disabled = false;
  }
}

async function stop(notifyEngine) {
  running = false;
  if (capture) {
    try {
      capture.processor.disconnect();
      capture.silence.disconnect();
    } catch (error) {
      // Already disconnected.
    }
    capture = null;
  }
  if (stream) {
    for (const track of stream.getTracks()) {
      track.stop();
    }
    stream = null;
  }
  if (playback) {
    await playback.close().catch(() => {});
    playback = null;
  }
  if (socket) {
    socket.onclose = null;
    socket.close();
    socket = null;
  }
  if (lease && notifyEngine !== false) {
    try {
      await request('voice/stop', { lease_id: lease.voice_id });
    } catch (error) {
      // The lease may already be gone.
    }
  }
  lease = null;
}

export function initVoice() {
  button.disabled = false;
  button.title = 'Talk to Goblin';
  button.addEventListener('click', async () => {
    if (running) {
      await stop(true);
      setButton(false);
    } else {
      await start();
    }
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && running) {
      stop(true);
      setButton(false);
    }
  });
}
