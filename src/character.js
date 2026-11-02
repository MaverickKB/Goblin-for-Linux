// Character renderer: the same rig, mesh deformation, face shader and motion
// constants as the Mac app, drawn with WebGL instead of Metal.

import { RIGS } from './character-rigs.js';

const COLUMNS = 40;
const ROWS = 80;

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
let renderer = null;

export function setCharacterSignals(partial) {
  signals = { ...signals, ...partial };
}

function smooth(value) {
  const fraction = Math.max(0, Math.min(1, value));
  return fraction * fraction * (3 - 2 * fraction);
}

function distanceToSegment(point, start, end, aspect) {
  const px = point.x * aspect;
  const py = point.y;
  const ax = start[0] * aspect;
  const ay = start[1];
  const bx = end[0] * aspect;
  const by = end[1];
  const segmentX = bx - ax;
  const segmentY = by - ay;
  const length = Math.max(segmentX * segmentX + segmentY * segmentY, 0.00001);
  const fraction = Math.max(0, Math.min(1, ((px - ax) * segmentX + (py - ay) * segmentY) / length));
  const closestX = ax + segmentX * fraction;
  const closestY = ay + segmentY * fraction;
  return Math.hypot(px - closestX, py - closestY);
}

function rotateAround(x, y, pivot, radians, aspect) {
  const deltaX = (x - pivot[0]) * aspect;
  const deltaY = y - pivot[1];
  const cos = Math.cos(radians);
  const sin = Math.sin(radians);
  return {
    x: pivot[0] + (deltaX * cos - deltaY * sin) / aspect,
    y: pivot[1] + (deltaX * sin + deltaY * cos),
  };
}

function mirroredArm(rig) {
  const axis = rig.pelvis[0];
  return rig.arm.map((point) => [2 * axis - point[0], point[1]]);
}

function buildMesh(rig, aspect) {
  const arm2 = mirroredArm(rig);
  const neckY = rig.neck[1];
  const shoulder = rig.arm[0];
  const elbow = rig.arm[1];
  const wrist = rig.arm[2];
  const shoulder2 = arm2[0];
  const elbow2 = arm2[1];
  const wrist2 = arm2[2];
  const vertices = [];

  for (let row = 0; row <= ROWS; row++) {
    for (let column = 0; column <= COLUMNS; column++) {
      const u = column / COLUMNS;
      const v = row / ROWS;
      const point = { x: u, y: 1 - v };

      const headWeight = smooth((neckY + 0.025 - point.y) / 0.085);
      const chestX = (point.x - rig.neck[0]) / 0.26;
      const chestY = (point.y - (neckY + 0.09)) / 0.16;
      const chestWeight = Math.exp(-chestX * chestX - chestY * chestY);

      const armDistance = Math.min(
        distanceToSegment(point, shoulder, elbow, aspect),
        distanceToSegment(point, elbow, wrist, aspect)
      );
      const forearmDistance = distanceToSegment(point, elbow, wrist, aspect);
      const belowShoulder = smooth((point.y - shoulder[1] + 0.015) / 0.06);
      const armWeight = Math.exp(-Math.pow(armDistance / rig.armRadius, 2)) * belowShoulder;
      const belowElbow = smooth((point.y - elbow[1] + 0.01) / 0.07);
      const forearmWeight = Math.exp(-Math.pow(forearmDistance / rig.armRadius, 2)) * belowElbow;

      const armDistance2 = Math.min(
        distanceToSegment(point, shoulder2, elbow2, aspect),
        distanceToSegment(point, elbow2, wrist2, aspect)
      );
      const forearmDistance2 = distanceToSegment(point, elbow2, wrist2, aspect);
      const belowShoulder2 = smooth((point.y - shoulder2[1] + 0.015) / 0.06);
      const arm2Weight = Math.exp(-Math.pow(armDistance2 / rig.armRadius, 2)) * belowShoulder2;
      const belowElbow2 = smooth((point.y - elbow2[1] + 0.01) / 0.07);
      const forearm2Weight = Math.exp(-Math.pow(forearmDistance2 / rig.armRadius, 2)) * belowElbow2;

      const mouthX = (point.x - rig.mouth[0]) / Math.max(rig.mouth[2], 0.01);
      const mouthY = (point.y - rig.mouth[1]) / Math.max(rig.mouth[3], 0.01);
      const jawWeight = Math.exp(-mouthX * mouthX * 1.8 - mouthY * mouthY * 0.7);
      const bodyWeight = smooth((0.88 - point.y) / 0.36);

      vertices.push({
        x: point.x,
        y: point.y,
        u,
        v,
        headWeight,
        bodyWeight,
        chestWeight,
        armWeight,
        forearmWeight,
        arm2Weight,
        forearm2Weight,
        jawWeight,
      });
    }
  }
  return { vertices, rig, aspect };
}

function destinationFor(vertex, mesh, pose) {
  const rig = mesh.rig;
  const aspect = mesh.aspect;
  const neck = rig.neck;
  const pelvis = rig.pelvis;
  const shoulder = rig.arm[0];
  const elbow = rig.arm[1];
  const arm2 = mirroredArm(rig);
  const shoulder2 = arm2[0];
  const elbow2 = arm2[1];

  let x = vertex.x;
  let y = vertex.y;

  const head = rotateAround(x, y, neck, pose.headAngle, aspect);
  x += (head.x - vertex.x + pose.headX) * vertex.headWeight;
  y += (head.y - vertex.y + pose.headY) * vertex.headWeight;
  y -= 0.0017 * pose.breath * vertex.chestWeight;
  x += (vertex.x - neck[0]) * 0.004 * pose.breath * vertex.chestWeight;

  let moved = rotateAround(x, y, elbow, pose.forearmAngle * vertex.forearmWeight, aspect);
  x = moved.x;
  y = moved.y;
  moved = rotateAround(x, y, shoulder, pose.armAngle * vertex.armWeight, aspect);
  x = moved.x;
  y = moved.y;
  moved = rotateAround(x, y, elbow2, pose.forearm2Angle * vertex.forearm2Weight, aspect);
  x = moved.x;
  y = moved.y;
  moved = rotateAround(x, y, shoulder2, pose.arm2Angle * vertex.arm2Weight, aspect);
  x = moved.x;
  y = moved.y;

  y += pose.jaw * rig.mouth[3] * 0.6 * vertex.jawWeight;

  const body = rotateAround(x, y, pelvis, pose.bodyAngle, aspect);
  x += (body.x - x + pose.bodyX) * vertex.bodyWeight;
  y += (body.y - y + pose.bodyY) * vertex.bodyWeight;

  return { x, y: 1 - y };
}

function createPose() {
  return {
    breath: 0,
    bodyX: 0,
    bodyY: 0,
    bodyAngle: 0,
    headAngle: 0,
    headX: 0,
    headY: 0,
    armAngle: 0,
    forearmAngle: 0,
    arm2Angle: 0,
    forearm2Angle: 0,
    blink: 0,
    gazeX: 0,
    gazeY: 0,
    jaw: 0,
    smile: 0,
    brow: 0,
  };
}

function addPose(target, addition, scale) {
  for (const key of Object.keys(addition)) {
    target[key] += addition[key] * scale;
  }
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
  const brow = createSpring();
  const squint = createSpring();
  const jaw = createSpring();

  let blinkStart = 5.3;
  let blinkIndex = 0;
  const blinkGaps = [6.7, 4.9, 8.2, 5.8, 7.4];
  let gestureStart = -10;
  let nextGesture = 2.4;
  let gestureIndex = 0;
  let gestureKind = 0;
  const gestureGaps = [6.1, 8.7, 7.3, 10.2, 6.8];
  let toolGestureStart = -10;
  let toolGestureKind = '';
  let lastActivityToken = 0;
  let completionStart = -10;

  function maxEngagement() {
    let result = listening.value;
    for (const spring of [thinking, working, speaking, concern, approval, autonomy, typing]) {
      result = Math.max(result, spring.value);
    }
    return result;
  }

  function smileTarget(current) {
    if (current.error) return -0.3;
    if (current.completed) return 0.4;
    if (current.speaking) return 0.25;
    if (current.listening) return 0.2;
    if (current.approval) return 0.1;
    if (current.working) return 0.1;
    if (current.thinking) return 0.05;
    return 0.12;
  }

  function browTarget(current) {
    if (current.error) return -0.2;
    if (current.approval) return 0.3;
    if (current.thinking) return 0.2;
    if (current.completed) return 0.2;
    return 0;
  }

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
      gestureKind = gestureIndex % 8;
      nextGesture = elapsed + duration + gestureGaps[gestureIndex % gestureGaps.length];
      gestureIndex += 1;
    }
    const progress = (elapsed - gestureStart) / duration;
    const pose = createPose();
    if (progress < 0 || progress > 1) return pose;
    const envelope = Math.pow(Math.sin(progress * Math.PI), 2);
    const turn = Math.sin(progress * 2 * Math.PI) * envelope;
    switch (gestureKind) {
      case 0:
        pose.headAngle = 0.16 * turn;
        pose.headX = 0.014 * turn;
        pose.gazeX = 0.65 * turn;
        pose.bodyX = 0.007 * turn;
        break;
      case 1:
        pose.armAngle = 0.14 * envelope;
        pose.forearmAngle = -0.08 * envelope;
        pose.headAngle = -0.055 * envelope;
        pose.bodyX = 0.008 * envelope;
        break;
      case 2:
        pose.bodyAngle = 0.045 * turn;
        pose.bodyX = 0.009 * turn;
        pose.headAngle = -0.075 * turn;
        pose.gazeX = -0.35 * turn;
        break;
      case 3:
        pose.armAngle = 0.16 * envelope;
        pose.forearmAngle = -0.1 * envelope;
        pose.arm2Angle = 0.16 * envelope;
        pose.forearm2Angle = -0.1 * envelope;
        pose.bodyY = 0.004 * envelope;
        pose.headAngle = -0.04 * envelope;
        break;
      case 4:
        pose.armAngle = 0.12 * envelope;
        pose.forearmAngle = 0.1 * envelope;
        pose.headAngle = 0.04 * envelope;
        pose.gazeY = -0.1 * envelope;
        pose.gazeX = 0.2 * envelope;
        break;
      case 5:
        pose.armAngle = 0.18 * envelope;
        pose.forearmAngle = 0.12 * envelope;
        pose.headAngle = -0.03 * envelope;
        pose.gazeY = 0.1 * envelope;
        break;
      case 6:
        pose.bodyY = 0.003 * envelope * Math.sin(progress * 2 * Math.PI * 3);
        pose.bodyAngle = 0.02 * envelope;
        pose.headAngle = 0.02 * envelope;
        break;
      default:
        pose.armAngle = 0.1 * envelope;
        pose.forearmAngle = 0.12 * envelope;
        pose.arm2Angle = 0.1 * envelope;
        pose.forearm2Angle = 0.12 * envelope;
        pose.bodyAngle = -0.02 * envelope;
    }
    return pose;
  }

  function toolGesturePose(current) {
    if (current.activityToken !== lastActivityToken) {
      lastActivityToken = current.activityToken;
      toolGestureStart = elapsed;
      toolGestureKind = current.toolKind;
    }
    const duration = 0.8;
    const progress = (elapsed - toolGestureStart) / duration;
    const pose = createPose();
    if (progress < 0 || progress > 1) return pose;
    const envelope = Math.pow(Math.sin(progress * Math.PI), 2);
    switch (toolGestureKind) {
      case 'shell':
        pose.armAngle = 0.16 * envelope * Math.sin(progress * 2 * Math.PI * 2);
        pose.forearmAngle = 0.18 * envelope * Math.sin(progress * 2 * Math.PI * 2 + 0.6);
        pose.bodyAngle = 0.015 * envelope;
        break;
      case 'read_file':
      case 'read':
        pose.forearmAngle = 0.1 * envelope * Math.sin(progress * Math.PI);
        pose.headAngle = 0.03 * envelope;
        pose.gazeY = 0.1 * envelope;
        break;
      case 'write_file':
      case 'write':
        pose.forearmAngle = 0.08 * envelope * Math.sin(progress * 2 * Math.PI * 3);
        pose.headAngle = 0.02 * envelope;
        break;
      case 'browser':
      case 'search':
      case 'web_search':
        pose.headAngle = 0.05 * envelope * Math.sin(progress * 2 * Math.PI);
        pose.gazeX = 0.3 * envelope;
        break;
      default:
        pose.armAngle = 0.06 * envelope;
        pose.forearmAngle = 0.08 * envelope;
    }
    return pose;
  }

  function completionGesturePose(current) {
    if (current.completed && completionStart < 0) {
      completionStart = elapsed;
    }
    if (!current.completed) {
      completionStart = -10;
      return createPose();
    }
    const duration = 1.2;
    const progress = (elapsed - completionStart) / duration;
    const pose = createPose();
    if (progress < 0 || progress > 1) return pose;
    const envelope = Math.pow(Math.sin(progress * Math.PI), 2);
    pose.headY = -0.006 * envelope;
    pose.headAngle = 0.03 * envelope;
    pose.bodyY = 0.002 * envelope;
    pose.smile = 0.15 * envelope;
    return pose;
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
    advanceSpring(smile, smileTarget(current), step, 0.4);
    advanceSpring(brow, browTarget(current), step, 0.35);
    advanceSpring(squint, current.thinking || current.error ? 0.5 : 0, step, 0.3);

    const speech = current.speaking ? Math.max(0, Math.min(current.speechLevel, 1)) : 0;
    advanceSpring(jaw, speech, step, 0.075);

    const breathing = Math.sin((elapsed * 2 * Math.PI) / 4.8);
    const secondary = Math.sin((elapsed * 2 * Math.PI) / 7.1);
    const workPhase = (elapsed * 2 * Math.PI) / 1.65;
    const workBeat = Math.sin(workPhase);
    const thoughtBeat = Math.sin((elapsed * 2 * Math.PI) / 5.6);
    const utterance = speaking.value * jaw.value;
    const engagement = maxEngagement();
    const idleWeight = Math.max(0, 1 - engagement);

    const pose = createPose();
    pose.breath = breathing;
    pose.bodyX = 0.003 * working.value * workBeat;
    pose.bodyAngle = 0.008 * working.value * Math.sin(workPhase * 0.5);
    pose.headAngle = 0.002 * secondary;
    pose.headAngle += 0.055 * listening.value;
    pose.headAngle -= 0.04 * thinking.value;
    pose.headAngle += 0.055 * thinking.value * thoughtBeat;
    pose.headAngle += 0.014 * working.value * workBeat;
    pose.headAngle += 0.018 * utterance;
    pose.headX = 0.004 * attention.value;
    pose.headX += 0.008 * thinking.value * thoughtBeat;
    pose.headY = -0.0015 * breathing;
    pose.headY += 0.003 * listening.value;
    pose.headY -= 0.008 * thinking.value;
    pose.headY += working.value * (-0.006 + 0.003 * workBeat);
    pose.armAngle = working.value * (0.085 + 0.065 * workBeat);
    pose.armAngle += 0.045 * thinking.value * (1 + thoughtBeat);
    pose.armAngle += 0.065 * utterance;
    pose.forearmAngle = working.value * (0.09 + 0.075 * Math.sin(workPhase + 1.1));
    pose.forearmAngle += 0.06 * thinking.value * (1 - thoughtBeat);
    pose.forearmAngle += 0.09 * utterance;
    pose.gazeX = 0.4 * attention.value;
    pose.gazeX += 0.28 * thinking.value * thoughtBeat;
    pose.gazeX += 0.24 * working.value;
    pose.gazeY = 0.18 * thinking.value;
    pose.gazeY -= 0.12 * listening.value;
    pose.gazeY += 0.14 * working.value;
    pose.jaw = jaw.value;

    pose.armAngle += 0.08 * thinking.value * (0.6 + 0.4 * thoughtBeat);
    pose.forearmAngle += 0.1 * thinking.value * (0.5 + 0.5 * thoughtBeat);
    pose.headAngle += 0.04 * thinking.value;
    pose.gazeY += 0.2 * thinking.value;
    pose.bodyAngle += 0.01 * working.value;
    pose.gazeY += 0.1 * working.value;
    pose.armAngle += 0.05 * utterance * (0.5 + 0.5 * Math.sin((elapsed * 2 * Math.PI) / 1.9));
    pose.forearmAngle += 0.07 * utterance;
    pose.headAngle += 0.02 * utterance;
    pose.headAngle += 0.06 * listening.value;
    pose.headX += 0.01 * listening.value;
    pose.bodyX += 0.006 * listening.value;
    pose.gazeX += 0.3 * listening.value;
    pose.gazeX += 0.3 * attention.value;
    pose.headX += 0.008 * attention.value;
    pose.bodyY -= 0.006 * concern.value;
    pose.headAngle -= 0.05 * concern.value;
    pose.headY -= 0.004 * concern.value;
    pose.armAngle -= 0.03 * concern.value;
    pose.forearmAngle -= 0.02 * concern.value;
    pose.headAngle += 0.015 * concern.value * Math.sin((elapsed * 2 * Math.PI) / 1.6);
    pose.bodyX += 0.006 * approval.value;
    pose.headAngle += 0.04 * approval.value;
    pose.armAngle += 0.08 * approval.value;
    pose.forearmAngle += 0.1 * approval.value;
    pose.gazeX += 0.3 * approval.value;
    pose.headAngle += 0.06 * autonomy.value * Math.sin((elapsed * 2 * Math.PI) / 6.2);
    pose.gazeX += 0.35 * autonomy.value * Math.sin((elapsed * 2 * Math.PI) / 5.0);
    pose.bodyAngle += 0.02 * autonomy.value * Math.sin((elapsed * 2 * Math.PI) / 7.0);
    pose.armAngle += 0.04 * autonomy.value;
    pose.bodyX += 0.005 * typing.value;
    pose.headAngle -= 0.02 * typing.value;
    pose.gazeX -= 0.25 * typing.value;
    pose.gazeY += 0.12 * typing.value;
    pose.arm2Angle += 0.03 * thinking.value * (0.6 + 0.4 * thoughtBeat);
    pose.forearm2Angle += 0.04 * thinking.value * (0.5 + 0.5 * thoughtBeat);
    pose.arm2Angle += 0.02 * working.value * workBeat;
    pose.forearm2Angle += 0.03 * working.value * Math.sin(workPhase + 1.1);
    pose.arm2Angle += 0.04 * utterance * (0.5 + 0.5 * Math.sin((elapsed * 2 * Math.PI) / 1.9 + 1.2));
    pose.forearm2Angle += 0.06 * utterance;
    pose.arm2Angle -= 0.03 * concern.value;
    pose.forearm2Angle -= 0.02 * concern.value;
    pose.arm2Angle += 0.04 * approval.value;
    pose.forearm2Angle += 0.06 * approval.value;
    pose.arm2Angle += 0.03 * autonomy.value * Math.sin((elapsed * 2 * Math.PI) / 6.2 + 0.8);

    addPose(pose, idleGesture(), idleWeight);
    addPose(pose, toolGesturePose(current), 1);
    addPose(pose, completionGesturePose(current), 1);
    pose.smile = smile.value;
    pose.brow = brow.value;
    pose.blink = Math.max(blinkAmount(), squint.value * 0.35);
    return pose;
  }

  return { advance };
}

const VERTEX_SHADER = `
attribute vec2 a_position;
attribute vec2 a_uv;
varying vec2 v_uv;
void main() {
  gl_Position = vec4(a_position, 0.0, 1.0);
  v_uv = a_uv;
}
`;

const FRAGMENT_SHADER = `
precision mediump float;
varying vec2 v_uv;
uniform sampler2D u_image;
uniform sampler2D u_eyelids;
uniform vec4 u_eye0;
uniform vec4 u_eye1;
uniform vec4 u_motion;
uniform vec4 u_mouth;
uniform vec4 u_expression;

vec4 eyeColor(vec2 uv, vec4 eye, vec4 original) {
  if (eye.z <= 0.0 || eye.w <= 0.0) {
    return original;
  }
  vec2 p = (uv - eye.xy) / eye.zw;
  float radius = max(abs(p.x), abs(p.y));
  if (radius > 1.15) {
    return original;
  }
  float edge = 1.0 - smoothstep(1.0, 1.15, radius);
  vec2 gaze = u_motion.yz * eye.zw * 0.25;
  vec4 looking = texture2D(u_image, uv - gaze);
  vec4 openEye = mix(original, looking, edge);
  if (u_motion.x <= 0.0) {
    return openEye;
  }
  float upper = -1.0 + u_motion.x * 1.5;
  float lower = 1.0 - u_motion.x * 0.5;
  float lid = 1.0 - smoothstep(upper - 0.08, upper + 0.08, p.y);
  lid = max(lid, smoothstep(lower - 0.06, lower + 0.06, p.y));
  vec4 closed = texture2D(u_eyelids, uv);
  return mix(openEye, closed, lid * edge);
}

void main() {
  if (u_motion.w > 0.0) {
    vec2 point = (v_uv - 0.5) * 2.0;
    float radius = dot(point, point);
    float alpha = exp(-radius * 3.0) * (1.0 - smoothstep(0.65, 1.0, radius)) * 0.22;
    gl_FragColor = vec4(0.0, 0.0, 0.0, alpha);
    return;
  }
  vec4 color = texture2D(u_image, v_uv);
  color = eyeColor(v_uv, u_eye0, color);
  color = eyeColor(v_uv, u_eye1, color);
  if (u_expression.x != 0.0) {
    vec2 mouthSize = max(u_mouth.zw, vec2(0.001));
    vec2 mouthUV = (v_uv - u_mouth.xy) / mouthSize;
    if (abs(mouthUV.x) <= 1.0 && abs(mouthUV.y) <= 1.0) {
      float curve = (mouthUV.x * mouthUV.x - 0.5) * u_expression.x * 0.02;
      vec2 warped = v_uv;
      warped.y -= curve;
      color = texture2D(u_image, warped);
    }
  }
  if (u_expression.y != 0.0) {
    vec4 eyes[2];
    eyes[0] = u_eye0;
    eyes[1] = u_eye1;
    for (int index = 0; index < 2; index++) {
      vec4 eye = eyes[index];
      vec2 eyeSize = max(eye.zw, vec2(0.001));
      vec2 browUV = (v_uv - eye.xy) / eyeSize;
      if (abs(browUV.x) <= 1.4 && browUV.y >= -2.2 && browUV.y <= -1.2) {
        vec2 warped = v_uv;
        warped.y -= u_expression.y * 0.008;
        color = texture2D(u_image, warped);
      }
    }
  }
  gl_FragColor = color;
}
`;

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('Could not load ' + url));
    image.src = url;
  });
}

function withTimeout(promise, milliseconds, message) {
  return Promise.race([
    promise,
    new Promise((resolve, reject) => {
      setTimeout(() => reject(new Error(message)), milliseconds);
    }),
  ]);
}

function compileShader(gl, type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    throw new Error(gl.getShaderInfoLog(shader) || 'The character shader did not compile.');
  }
  return shader;
}

function buildProgram(gl) {
  const program = gl.createProgram();
  gl.attachShader(program, compileShader(gl, gl.VERTEX_SHADER, VERTEX_SHADER));
  gl.attachShader(program, compileShader(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER));
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    throw new Error(gl.getProgramInfoLog(program) || 'The character program did not link.');
  }
  return program;
}

function indicesFor() {
  const indices = [];
  for (let row = 0; row < ROWS; row++) {
    for (let column = 0; column < COLUMNS; column++) {
      const a = row * (COLUMNS + 1) + column;
      const b = a + 1;
      const c = a + (COLUMNS + 1);
      indices.push(a, b, c, b, c + 1, c);
    }
  }
  return new Uint16Array(indices);
}

function textureFor(gl, image) {
  const texture = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, texture);
  gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
  return texture;
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

export async function initCharacter(canvas, options = {}) {
  const avatar = options.avatar || 'rookie';
  const rig = RIGS.avatars[avatar];
  if (!rig) {
    throw new Error('No character rig for ' + avatar);
  }
  const standing = await withTimeout(loadImage(STANDING_URL + avatar + '.png'), 6000,
    'The standing art did not load.');
  const eyelids = await withTimeout(loadImage(EYELIDS_URL + avatar + '.png'), 6000,
    'The eyelid art did not load.');

  const gl = canvas.getContext('webgl', {
    alpha: true,
    antialias: true,
    premultipliedAlpha: true,
  });
  if (!gl) {
    throw new Error('WebGL is unavailable.');
  }

  const aspect = standing.width / standing.height;
  const mesh = buildMesh(rig, aspect);
  const program = buildProgram(gl);
  const positionBuffer = gl.createBuffer();
  const uvBuffer = gl.createBuffer();
  const indexBuffer = gl.createBuffer();
  const quadBuffer = gl.createBuffer();
  const imageTexture = textureFor(gl, standing);
  const eyelidTexture = textureFor(gl, eyelids);

  const positions = new Float32Array(mesh.vertices.length * 2);
  const uvs = new Float32Array(mesh.vertices.length * 2);
  for (let index = 0; index < mesh.vertices.length; index++) {
    uvs[index * 2] = mesh.vertices[index].u;
    uvs[index * 2 + 1] = mesh.vertices[index].v;
  }
  gl.bindBuffer(gl.ARRAY_BUFFER, uvBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, uvs, gl.STATIC_DRAW);
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, indexBuffer);
  gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, indicesFor(), gl.STATIC_DRAW);
  gl.bindBuffer(gl.ARRAY_BUFFER, quadBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
    -0.3, -1.0, 0.0, 1.0,
    0.3, -1.0, 1.0, 1.0,
    -0.3, -0.7, 0.0, 0.0,
    0.3, -0.7, 1.0, 0.0,
  ]), gl.STATIC_DRAW);

  const locations = {
    position: gl.getAttribLocation(program, 'a_position'),
    uv: gl.getAttribLocation(program, 'a_uv'),
    image: gl.getUniformLocation(program, 'u_image'),
    eyelids: gl.getUniformLocation(program, 'u_eyelids'),
    eye0: gl.getUniformLocation(program, 'u_eye0'),
    eye1: gl.getUniformLocation(program, 'u_eye1'),
    motion: gl.getUniformLocation(program, 'u_motion'),
    mouth: gl.getUniformLocation(program, 'u_mouth'),
    expression: gl.getUniformLocation(program, 'u_expression'),
  };

  const motion = createMotion();
  let lastTime = 0;
  let frame = 0;

  function render(now) {
    frame = requestAnimationFrame(render);
    if (document.hidden) {
      lastTime = now;
      return;
    }
    const delta = lastTime > 0 ? (now - lastTime) / 1000 : 1 / 60;
    lastTime = now;
    const pose = motion.advance(delta, signals);
    const destination = mesh.vertices.map((vertex) => destinationFor(vertex, mesh, pose));

    fitCanvas(canvas);
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.useProgram(program);

    const scale = Math.min(canvas.width / standing.width, canvas.height / standing.height);
    const width = standing.width * scale;
    const height = standing.height * scale;
    const left = (canvas.width - width) / 2;

    for (let index = 0; index < destination.length; index++) {
      positions[index * 2] = ((left + destination[index].x * width) / canvas.width) * 2 - 1;
      positions[index * 2 + 1] = (destination[index].y * height / canvas.height) * 2 - 1;
    }

    gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, positions, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(locations.position);
    gl.vertexAttribPointer(locations.position, 2, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, uvBuffer);
    gl.enableVertexAttribArray(locations.uv);
    gl.vertexAttribPointer(locations.uv, 2, gl.FLOAT, false, 0, 0);

    gl.uniform4f(locations.eye0, rig.eyes[0] ? rig.eyes[0][0] : 0, rig.eyes[0] ? rig.eyes[0][1] : 0,
      rig.eyes[0] ? rig.eyes[0][2] : 0, rig.eyes[0] ? rig.eyes[0][3] : 0);
    gl.uniform4f(locations.eye1, rig.eyes[1] ? rig.eyes[1][0] : 0, rig.eyes[1] ? rig.eyes[1][1] : 0,
      rig.eyes[1] ? rig.eyes[1][2] : 0, rig.eyes[1] ? rig.eyes[1][3] : 0);
    gl.uniform4f(locations.mouth, rig.mouth[0], rig.mouth[1], rig.mouth[2], rig.mouth[3]);
    gl.uniform4f(locations.expression, pose.smile, pose.brow, 0, 0);

    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, imageTexture);
    gl.uniform1i(locations.image, 0);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, eyelidTexture);
    gl.uniform1i(locations.eyelids, 1);

    gl.uniform4f(locations.motion, 0, 0, 0, 1);
    gl.bindBuffer(gl.ARRAY_BUFFER, quadBuffer);
    gl.enableVertexAttribArray(locations.position);
    gl.vertexAttribPointer(locations.position, 2, gl.FLOAT, false, 16, 0);
    gl.enableVertexAttribArray(locations.uv);
    gl.vertexAttribPointer(locations.uv, 2, gl.FLOAT, false, 16, 8);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);

    gl.uniform4f(locations.motion, pose.blink, pose.gazeX, pose.gazeY, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
    gl.enableVertexAttribArray(locations.position);
    gl.vertexAttribPointer(locations.position, 2, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, uvBuffer);
    gl.enableVertexAttribArray(locations.uv);
    gl.vertexAttribPointer(locations.uv, 2, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, indexBuffer);
    gl.drawElements(gl.TRIANGLES, (COLUMNS * ROWS * 6), gl.UNSIGNED_SHORT, 0);
  }

  renderer = { frame: 0 };
  frame = requestAnimationFrame(render);
  renderer.frame = frame;
  return { stop: () => cancelAnimationFrame(frame) };
}

export function stopCharacter() {
  if (renderer) {
    cancelAnimationFrame(renderer.frame);
    renderer = null;
  }
}
