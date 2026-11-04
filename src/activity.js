// Maps an engine activity phase to what the window should show. Pure, so the
// chat surface can be checked without a browser. live null means say nothing.

export function describeActivity(phase, data) {
  const text = (data && data.text) || '';
  if (phase === 'ready') {
    return { live: { text: 'Ready', detail: '' }, character: { working: false, thinking: false } };
  }
  if (phase === 'heartbeat') {
    return { live: null, character: {} };
  }
  if (phase === 'model_start') {
    return { live: { text: 'Thinking', detail: '' }, character: { thinking: true, working: false } };
  }
  if (phase === 'tool') {
    const toolName = String(text).trim().split(/\s+/)[0] || 'tool';
    return {
      live: { text: 'Working', detail: text },
      character: { working: true, thinking: false, toolKind: toolName, bumpToken: true },
    };
  }
  if (phase === 'compacting') {
    // Housekeeping. The settings panel reports it; chat stays silent.
    return { live: null, character: {} };
  }
  if (phase === 'status' && text) {
    return { live: { text, detail: '' }, character: {} };
  }
  return { live: null, character: {} };
}
