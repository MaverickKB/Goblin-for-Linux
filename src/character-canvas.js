// Character renderer for Linux. It uses the same art, blink timing and idle
// rhythm as the Mac app, drawn with Canvas 2D. The Mac's mesh deformation is a
// later upgrade; this renderer keeps the body motion and eyelids.

const STANDING_URL = 'assets/character/standing/';
const EYELIDS_URL = 'assets/character/eyelids/';

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

function smooth(value) {
  const fraction = Math.max(0, Math.min(1, value));
  return fraction * fraction * (3 - 2 * fraction);
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('Could not load ' + url));
    image.src = url;
  });
}

function createSpring(value = 0) {
  return { value, velocity: 0 };
}

function advanceSpring(spring, target, seconds, response) {
  const omega = 2 / response;
  const offset = spring.value - target;
  const decay = Math.exp(-omega * seconds);
  const change = (spring.velocity + omega * offset) * seconds;
  spring.value = target + (offset + change) * decay;
  spring.velocity = (spring.velocity - omega * change) * decay;
}

function createMotion() {
  let elapsed = 0;
  const listening = createSpring();
  const thinking = createSpring();
  const working = createSpring();
  const speaking = createSpring();
  const attention = createSpring();
  const concern = createSpring();
  const approval = createSpring();
  const autonomy = createSpring();
  const typing = createSpring();
  const smile = createSpring();

  let blinkStart = 5.3;
  let blinkIndex = 0;
  const blinkGaps = [6.7, 4.9, 8.2, 5.8, 7.4];
  let gestureStart = -10;
  let nextGesture = 2.4;
  let gestureIndex = 0;
  let completionStart = -10;

  function blinkAmount() {
    const duration = 0.21;
    if (elapsed > blinkStart + duration) {
      blinkStart = elapsed + blinkGaps[blinkIndex % blinkGaps.length];
      blinkIndex += 1;
    }
    const time = elapsed - blinkStart;
    if (time < 0 || time > duration) return 0;
    if (time < 0.075) return smooth(time / 0.075);
    if (time < 0.105) return 1;
    return 1 - smooth((time - 0.105) / 0.105);
  }

  function idleGesture() {
    const duration = 3.2;
    if (elapsed >= nextGesture) {
      gestureStart = elapsed;
      gestureIndex += 1;
      nextGesture = elapsed + duration + [6.1, 8.7, 7.3, 10.2, 6.8][gestureIndex % 5];
    }
    const progress = (elapsed - gestureStart) / duration;
    if (progress < 0 || progress > 1) return { turn: 0, envelope: 0, kind: 0 };
    const envelope = Math.pow(Math.sin(progress * Math.PI), 2);
    return {
      turn: Math.sin(progress * 2 * Math.PI) * envelope,
      envelope,
      kind: gestureIndex % 4,
    };
  }

  function advance(seconds, current) {
    const step = Math.max(0, Math.min(seconds, 1 / 15));
    elapsed += step;

    advanceSpring(listening, current.listening ? 1 : 0, step, 0.34);
    advanceSpring(thinking, current.thinking ? 1 : 0, step, 0.65);
    advanceSpring(working, current.working ? 1 : 0, step, 0.55);
    advanceSpring(speaking, current.speaking ? 1 : 0, step, 0.4);
    advanceSpring(attention, current.attention ? 1 : 0, step, 0.45);
    advanceSpring(concern, current.error ? 1 : 0, step, 0.5);
    advanceSpring(approval, current.approval ? 1 : 0, step, 0.4);
    advanceSpring(autonomy, current.autonomy ? 1 : 0, step, 0.6);
    advanceSpring(typing, current.typing ? 1 : 0, step, 0.3);

    let smileTarget = 0.12;
    if (current.error) smileTarget = -0.3;
    else if (current.completed) smileTarget = 0.4;
    else if (current.speaking) smileTarget = 0.25;
    else if (current.listening) smileTarget = 0.2;
    advanceSpring(smile, smileTarget, step, 0.4);

    const breathing = Math.sin((elapsed * 2 * Math.PI) / 4.8);
    const slow = Math.sin((elapsed * 2 * Math.PI) / 7.1);
    const work = Math.sin((elapsed * 2 * Math.PI) / 1.65);
    const thought = Math.sin((elapsed * 2 * Math.PI) / 5.6);
    const gesture = idleGesture();

    const pose = {
      breath: breathing,
      bodyX: 0.003 * working.value * work + 0.006 * listening.value + 0.005 * typing.value,
      bodyY: -0.006 * concern.value,
      bodyAngle: 0.008 * working.value * Math.sin((elapsed * 2 * Math.PI) / 3.3),
      headAngle: 0.002 * slow + 0.055 * listening.value - 0.04 * thinking.value
        + 0.055 * thinking.value * thought + 0.014 * working.value * work
        + 0.018 * speaking.value - 0.05 * concern.value + 0.04 * approval.value
        + 0.06 * autonomy.value * Math.sin((elapsed * 2 * Math.PI) / 6.2),
      gazeX: 0.4 * attention.value + 0.28 * thinking.value * thought
        + 0.24 * working.value + 0.3 * listening.value, 
      gazeY: 0.18 * thinking.value - 0.12 * listening.value + 0.14 * working.value,
      blink: blinkAmount(),
      smile: smile.value,
    };

    if (current.completed && completionStart < 0) completionStart = elapsed;
    if (!current.completed) completionStart = -10;
    if (completionStart >= 0) {
      const progress = (elapsed - completionStart) / 1.2;
      if (progress >= 0 && progress <= 1) {
        const envelope = Math.pow(Math.sin(progress * Math.PI), 2);
        pose.bodyY -= 0.006 * envelope;
        pose.headAngle += 0.03 * envelope;
        pose.smile += 0.15 * envelope;
      }
    }

    const idleWeight = Math.max(0, 1 - Math.max(listening.value, thinking.value,
      working.value, speaking.value, approval.value, typing.value));
    pose.headAngle += 0.16 * gesture.turn * idleWeight;
    pose.bodyX += 0.007 * gesture.turn * idleWeight;
    pose.gazeX += 0.65 * gesture.turn * idleWeight;
    pose.bodyAngle += 0.045 * gesture.turn * idleWeight * (gesture.kind === 2 ? 1 : 0);
    pose.bodyY += 0.003 * gesture.envelope * idleWeight * (gesture.kind === 3 ? 1 : 0);

    return pose;
  }

  return { advance };
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

export async function initCanvasCharacter(canvas, options = {}) {
  const avatar = options.avatar || 'rookie';
  const standing = await loadImage(STANDING_URL + avatar + '.png');
  const eyelids = await loadImage(EYELIDS_URL + avatar + '.png');
  const context = canvas.getContext('2d');
  if (!context) {
    throw new Error('Canvas 2D is unavailable.');
  }

  const motion = createMotion();
  let lastTime = 0;
  let frame = 0;

  function render(now) {
    frame = requestAnimationFrame(render);
    const delta = lastTime > 0 ? (now - lastTime) / 1000 : 1 / 60;
    lastTime = now;
    if (document.hidden) return;

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

    context.save();
    context.globalAlpha = 0.22;
    context.fillStyle = '#000000';
    context.beginPath();
    context.ellipse(width / 2, height - 4, drawWidth * 0.34, drawHeight * 0.02, 0, 0, Math.PI * 2);
    context.fill();
    context.restore();

    context.save();
    context.translate(
      left + drawWidth / 2 + pose.bodyX * drawWidth,
      top + drawHeight + pose.bodyY * drawHeight
    );
    context.rotate(pose.bodyAngle + pose.headAngle * 0.6);
    context.scale(1 + 0.004 * pose.breath, 1 + 0.004 * pose.breath);
    context.drawImage(standing, -drawWidth / 2, -drawHeight, drawWidth, drawHeight);
    if (pose.blink > 0.02) {
      context.globalAlpha = Math.min(1, pose.blink);
      context.drawImage(eyelids, -drawWidth / 2, -drawHeight, drawWidth, drawHeight);
    }
    context.restore();
  }

  frame = requestAnimationFrame(render);
  return { stop: () => cancelAnimationFrame(frame) };
}
