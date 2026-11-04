// Provider model catalogs. These ids are the ones the provider documents, so
// the dropdown can seed itself before a network catalog arrives. The fetched
// catalog is merged on top, never trusted as the only source.

export const CUSTOM_PROVIDER = 'custom';
export const COMPACTION_PROVIDER = 'xai';
export const COMPACTION_MODEL = 'grok-4.20-0309-non-reasoning';

// reasoning is true, false, or null when the provider does not document it.
const XAI_MODELS = [
  { id: 'grok-4.7', name: 'Grok 4.7', reasoning: true },
  { id: 'grok-4.6', name: 'Grok 4.6', reasoning: null },
  { id: 'grok-4.5', name: 'Grok 4.5', reasoning: null },
  { id: 'grok-4.3', name: 'Grok 4.3', reasoning: null },
  { id: 'grok-4.20-0309-reasoning', name: 'Grok 4.20 (reasoning)', reasoning: true },
  { id: 'grok-4.20-0309-non-reasoning', name: 'Grok 4.20 (non-reasoning)', reasoning: false },
  { id: 'grok-4.20-multi-agent-0309', name: 'Grok 4.20 (multi-agent)', reasoning: null },
  { id: 'grok-build-0.1', name: 'Grok Build 0.1', reasoning: null },
];

export const PROVIDER_CATALOG = {
  xai: { name: 'xAI', models: XAI_MODELS },
};

export function catalogFor(provider) {
  const entry = PROVIDER_CATALOG[provider];
  return entry ? entry.models : [];
}

export function providerName(provider) {
  if (provider === CUSTOM_PROVIDER) {
    return 'Custom endpoint';
  }
  const entry = PROVIDER_CATALOG[provider];
  return entry ? entry.name : provider;
}

function mergedModels(provider, fetched) {
  const byId = new Map();
  for (const model of catalogFor(provider)) {
    byId.set(model.id, { id: model.id, name: model.name, reasoning: model.reasoning });
  }
  for (const model of fetched || []) {
    if (!model || typeof model.id !== 'string' || !model.id) {
      continue;
    }
    const known = byId.get(model.id);
    byId.set(model.id, {
      id: model.id,
      name: model.name || model.id,
      reasoning: known ? known.reasoning : null,
    });
  }
  return [...byId.values()];
}

export function modelOptions(provider, fetched, savedId) {
  const models = mergedModels(provider, fetched);
  const options = models.map((model) => ({
    id: model.id,
    name: model.name,
    reasoning: model.reasoning,
    saved: model.id === savedId,
    known: true,
  }));
  if (savedId && !models.some((model) => model.id === savedId)) {
    options.unshift({ id: savedId, name: savedId, reasoning: null, saved: true, known: false });
  }
  return options;
}

function normalize(id) {
  return String(id).toLowerCase().replace(/[^a-z0-9]/g, '');
}

function tokens(id) {
  return String(id).toLowerCase().split(/[^a-z0-9.]+/).filter(Boolean);
}

function commonPrefix(left, right) {
  let index = 0;
  while (index < left.length && index < right.length && left[index] === right[index]) {
    index += 1;
  }
  return index;
}

export function closestMatch(provider, id) {
  const target = normalize(id);
  if (!target) {
    return '';
  }
  const wanted = new Set(tokens(id));
  let best = '';
  let bestScore = -1;
  for (const model of catalogFor(provider)) {
    const candidate = normalize(model.id);
    if (candidate === target) {
      return model.id;
    }
    // Distinguishing words matter more than a shared prefix: "non-reasoning"
    // and "reasoning" share almost everything before the difference.
    const matched = tokens(model.id).filter((token) => wanted.has(token)).length;
    const score = matched * 10 + commonPrefix(candidate, target) / Math.max(candidate.length, 1);
    if (score > bestScore) {
      bestScore = score;
      best = model.id;
    }
  }
  return bestScore >= 20 ? best : '';
}
