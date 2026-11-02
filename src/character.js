// Character entry point: prefer the Mac mesh renderer, fall back to Canvas 2D.

import { initWebGLCharacter, setCharacterSignals as setWebGLSignals } from './character-webgl.js';
import { initCanvasCharacter, setCharacterSignals as setCanvasSignals } from './character-canvas.js';

export function setCharacterSignals(partial) {
  setWebGLSignals(partial);
  setCanvasSignals(partial);
}

function webglAvailable() {
  try {
    const probe = document.createElement('canvas');
    const gl = probe.getContext('webgl', { failIfMajorPerformanceCaveat: false });
    if (!gl) {
      return false;
    }
    const version = gl.getParameter(gl.VERSION);
    return typeof version === 'string' && version.length > 0;
  } catch (error) {
    return false;
  }
}

export async function initCharacter(canvas, options = {}) {
  if (webglAvailable()) {
    try {
      const renderer = await initWebGLCharacter(canvas, options);
      document.body.dataset.renderer = 'webgl';
      return renderer;
    } catch (error) {
      // Fall through to the 2D renderer.
    }
  }
  document.body.dataset.renderer = 'canvas';
  return initCanvasCharacter(canvas, options);
}
