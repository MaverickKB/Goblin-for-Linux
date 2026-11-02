// Canvas 2D mesh renderer: Mac rig deformation without WebGL.

import { RIGS } from './character-rigs.js';
import { buildMesh, destinationFor, createMotion } from './character-webgl.js';

const STANDING_URL = 'assets/character/standing/';
const EYELIDS_URL = 'assets/character/eyelids/';
const COLUMNS = 12;
const ROWS = 24;

const SIGNAL_DEFAULTS = {
  listening: false,
  thinking: false,
  working: false,
  speaking: false,
  attention: false,
  speechLevel: 0,
  error: false,
  approval: false,
  autonomy: false,
  typing: false,
  toolKind: '',
  activityToken: 0,
  completed: false,
};

let signals = { ...SIGNAL_DEFAULTS };

export function setCharacterSignals(partial) {
  signals = { ...signals, ...partial };
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('Could not load ' + url));
    image.src = url;
  });
}

function fitCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(canvas.clientWidth * ratio));
  const height = Math.max(1, Math.round(canvas.clientHeight * ratio));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
}

function drawTriangle(context, image, sourceX, destination) {
  const [s0, s1, s2] = sourceX;
  const [d0, d1, d2] = destination;
  const denominator = s0.x * (s1.y - s2.y) + s1.x * (s2.y - s0.y) + s2.x * (s0.y - s1.y);
  if (Math.abs(denominator) < 0.000001) {
    return;
  }
  const a = (d0.x * (s1.y - s2.y) + d1.x * (s2.y - s0.y) + d2.x * (s0.y - s1.y)) / denominator;
  const b = (d0.y * (s1.y - s2.y) + d1.y * (s2.y - s0.y) + d2.y * (s0.y - s1.y)) / denominator;
  const c = (d0.x * (s2.x - s1.x) + d1.x * (s0.x - s2.x) + d2.x * (s1.x - s0.x)) / denominator;
  const d = (d0.y * (s2.x - s1.x) + d1.y * (s0.x - s2.x) + d2.y * (s1.x - s0.x)) / denominator;
  const e = (d0.x * (s1.x * s2.y - s2.x * s1.y) + d1.x * (s2.x * s0.y - s0.x * s2.y)
    + d2.x * (s0.x * s1.y - s1.x * s0.y)) / denominator;
  const f = (d0.y * (s1.x * s2.y - s2.x * s1.y) + d1.y * (s2.x * s0.y - s0.x * s2.y)
    + d2.y * (s0.x * s1.y - s1.x * s0.y)) / denominator;

  context.save();
  context.beginPath();
  const centerX = (d0.x + d1.x + d2.x) / 3;
  const centerY = (d0.y + d1.y + d2.y) / 3;
  const points = [d0, d1, d2].map((point) => {
    const deltaX = point.x - centerX;
    const deltaY = point.y - centerY;
    const length = Math.hypot(deltaX, deltaY) || 1;
    const grow = (length + 0.8) / length;
    return { x: centerX + deltaX * grow, y: centerY + deltaY * grow };
  });
  context.moveTo(points[0].x, points[0].y);
  context.lineTo(points[1].x, points[1].y);
  context.lineTo(points[2].x, points[2].y);
  context.closePath();
  context.clip();
  context.transform(a, b, c, d, e, f);
  context.drawImage(image, 0, 0);
  context.restore();
}

export async function initCanvasCharacter(canvas, options = {}) {
  const avatar = options.avatar || 'rookie';
  const rig = RIGS.avatars[avatar];
  if (!rig) {
    throw new Error('No character rig for ' + avatar);
  }
  const standing = await loadImage(STANDING_URL + avatar + '.png');
  const eyelids = await loadImage(EYELIDS_URL + avatar + '.png');
  const context = canvas.getContext('2d');
  if (!context) {
    throw new Error('Canvas 2D is unavailable.');
  }

  const motion = createMotion();
  const mesh = buildMesh(rig, standing.width / standing.height, COLUMNS, ROWS);
  const vertices = mesh.vertices;
  const columns = mesh.columns;
  let lastTime = 0;
  let lastDraw = 0;
  let frame = 0;

  function render(now) {
    frame = requestAnimationFrame(render);
    const delta = lastTime > 0 ? (now - lastTime) / 1000 : 1 / 60;
    lastTime = now;
    if (document.hidden || now - lastDraw < 55) {
      return;
    }
    lastDraw = now;

    const pose = motion.advance(delta, signals);
    fitCanvas(canvas);
    const width = canvas.width;
    const height = canvas.height;
    context.clearRect(0, 0, width, height);

    const scale = Math.min(width / standing.width, height / standing.height) * 0.96;
    const drawWidth = standing.width * scale;
    const drawHeight = standing.height * scale;
    const left = (width - drawWidth) / 2;
    const top = height - drawHeight;

    const destination = vertices.map((vertex) => {
      const point = destinationFor(vertex, mesh, pose);
      return { x: left + point.x * drawWidth, y: top + point.y * drawHeight };
    });
    const source = vertices.map((vertex) => ({
      x: vertex.u * standing.width,
      y: vertex.v * standing.height,
    }));

    context.save();
    context.globalAlpha = 0.2;
    context.fillStyle = '#000000';
    context.beginPath();
    context.ellipse(width / 2, height - 4, drawWidth * 0.34, drawHeight * 0.02, 0, 0, Math.PI * 2);
    context.fill();
    context.restore();

    for (let row = 0; row < ROWS; row++) {
      for (let column = 0; column < columns; column++) {
        const a = row * (columns + 1) + column;
        const b = a + 1;
        const c = a + (columns + 1);
        const d = c + 1;
        drawTriangle(context, standing,
          [source[a], source[b], source[c]], [destination[a], destination[b], destination[c]]);
        drawTriangle(context, standing,
          [source[b], source[d], source[c]], [destination[b], destination[d], destination[c]]);
      }
    }

    if (pose.blink > 0.03) {
      context.save();
      context.globalAlpha = Math.min(1, pose.blink);
      for (let row = 0; row < ROWS; row++) {
        for (let column = 0; column < columns; column++) {
          const a = row * (columns + 1) + column;
          const b = a + 1;
          const c = a + (columns + 1);
          const d = c + 1;
          drawTriangle(context, eyelids,
            [source[a], source[b], source[c]], [destination[a], destination[b], destination[c]]);
          drawTriangle(context, eyelids,
            [source[b], source[d], source[c]], [destination[b], destination[d], destination[c]]);
        }
      }
      context.restore();
    }
  }

  frame = requestAnimationFrame(render);
  return { stop: () => cancelAnimationFrame(frame) };
}
