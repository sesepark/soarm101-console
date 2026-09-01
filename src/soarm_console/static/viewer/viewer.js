// SO-ARM101 가상 리더 뷰어.
//
// 이 파일 하나가 두 기기를 모두 그린다.
//
// - `?host=web`  — 아이폰. 이 페이지가 WebSocket을 직접 열고, 관절 슬라이더·카메라·정지
//                  버튼까지 자기 화면에 그린다.
// - `?host=native` — 맥. 3D만 그리고 나머지는 네이티브 SwiftUI가 맡는다. 전송도 앱이
//                  하고, 이 페이지는 `window.webkit.messageHandlers.soarm`으로 목표를
//                  올리고 `window.soarmViewer.*`로 상태를 받아 그린다.
//
// 3D를 두 번 만들지 않는 것이 요점이다. 구현이 둘이면 두 기기가 같은 팔에 대해 서로 다른
// 그림을 그리게 되고, 그 차이는 조작하는 순간에만 드러난다.
import * as THREE from 'three';
import { OrbitControls } from 'three/OrbitControls.js';
import { loadURDF } from './urdf-mini.js';

const params = new URLSearchParams(location.search);
const HOST = params.get('host') === 'native' ? 'native' : 'web';
const SESSION = `${HOST}-${Math.random().toString(36).slice(2, 10)}`;
const COMMAND_HZ = 30;
/** 화면에 뜨는 조작자 이름. 리스를 누가 쥐고 있는지 다른 기기에서 읽을 수 있어야 한다. */
const HOLDER =
  params.get('holder') ||
  (matchMedia('(pointer: coarse)').matches ? '아이폰' : '브라우저');

document.body.classList.add(`host-${HOST}`);

const el = (id) => document.getElementById(id);
const canvas = el('canvas');

// ---------------------------------------------------------------- 3D 무대

const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(42, 1, 0.01, 40);
camera.position.set(0.45, 0.34, 0.45);
const controls = new OrbitControls(camera, canvas);
controls.target.set(0, 0.12, 0);
controls.enableDamping = true;
controls.minDistance = 0.16;
controls.maxDistance = 1.6;

/** 모델을 화면 안에 담는다. 팔의 크기를 코드에 적어 두면 URDF를 바꾸는 순간 틀린다. */
function frameModel(object) {
  const box = new THREE.Box3().setFromObject(object);
  if (box.isEmpty()) return;
  const center = box.getCenter(new THREE.Vector3());
  const radius = box.getSize(new THREE.Vector3()).length() / 2;
  const distance = (radius / Math.sin((camera.fov * Math.PI) / 360)) * 1.15;
  controls.target.copy(center);
  camera.position.copy(center).add(new THREE.Vector3(0.62, 0.5, 0.62).normalize().multiplyScalar(distance));
  controls.minDistance = radius * 0.6;
  controls.maxDistance = distance * 3;
  camera.near = Math.max(0.01, distance / 100);
  camera.far = distance * 20;
  camera.updateProjectionMatrix();
  controls.update();
  // 바닥 격자를 팔의 밑동에 맞춘다.
  grid.position.y = box.min.y;
}

scene.add(new THREE.HemisphereLight(0xdfe7ff, 0x0b1018, 1.15));
const key = new THREE.DirectionalLight(0xffffff, 1.5);
key.position.set(0.6, 1.0, 0.5);
scene.add(key);
const grid = new THREE.GridHelper(1.2, 12, 0x2a3550, 0x161d2c);
grid.material.transparent = true;
grid.material.opacity = 0.6;
scene.add(grid);

function resize() {
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  renderer.setSize(rect.width, rect.height, false);
  camera.aspect = rect.width / rect.height;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(canvas);

// ---------------------------------------------------------------- 상태

/** 서버가 내려준 관절 계약. 한계도 이름도 여기서만 온다 — UI에 박아 두지 않는다. */
let spec = [];
let specByName = new Map();
/** 서버가 말한 실제 관절값. */
let present = {};
/** 우리가 보내고 있는 목표. 조작하지 않는 동안에는 계속 실제값을 따라간다. */
let target = {};
let telemetry = null;
let lease = null;
let policy = {};
let live = null; // 로봇 모델(실측)
let ghost = null; // 목표 모델(반투명)
let grabbing = null; // 지금 끌고 있는 관절 이름
let commanding = false;
let sequence = 0;
/** 첫 텔레메트리가 오면 그 자세로 한 번 화면을 맞춘다. 0도 자세로 맞춰 두면 실제 팔이
    접혀 있을 때 모델이 화면 한쪽으로 밀린 채 시작한다. */
let framed = false;

const NATIVE = HOST === 'native' ? window.webkit?.messageHandlers?.soarm : null;

function toNative(message) {
  try {
    NATIVE?.postMessage(message);
  } catch (error) {
    console.warn('native bridge unavailable', error);
  }
}

// 맥 앱 안에서는 이 페이지가 혼자 아무것도 하지 않는다. 다리가 없으면 3D는 영원히 0도
// 자세로 서 있게 되는데, 그 화면은 "팔이 그 자세다"라고 거짓말을 한다. 그래서 다리가
// 없으면 페이지가 스스로 그렇게 말한다.
if (HOST === 'native' && !NATIVE) {
  document.getElementById('boot-detail').textContent =
    '앱과 3D 사이의 연결이 없습니다 (webkit.messageHandlers.soarm 없음)';
  document.body.classList.add('loading');
}

function radiansFor(name, value) {
  const item = specByName.get(name);
  if (!item) return 0;
  return (item.urdf_sign || 1) * value * item.radians_per_unit;
}

function applyPose(model, values, fallback) {
  if (!model) return;
  for (const item of spec) {
    const raw = values[item.name];
    const value = raw === undefined ? fallback?.[item.name] : raw;
    if (value === undefined) continue;
    model.setJoint(item.urdf_joint, radiansFor(item.name, value));
  }
}

// ---------------------------------------------------------------- 조작

/** 화면 위 드래그를 관절 각도로. 관절의 회전축을 화면에 투영해 그 수직 성분만 쓴다. */
function dragToDelta(jointName, dx, dy) {
  const joint = live.joints.get(specByName.get(jointName)?.urdf_joint);
  if (!joint) return 0;
  const origin = new THREE.Vector3().setFromMatrixPosition(joint.pivot.matrixWorld);
  const axis = joint.axis
    .clone()
    .transformDirection(joint.pivot.matrixWorld)
    .normalize();
  const tip = origin.clone().add(axis.multiplyScalar(0.05));
  const a = origin.clone().project(camera);
  const b = tip.project(camera);
  // 축이 화면 쪽을 보고 있으면(=투영이 짧으면) 드래그를 회전으로 읽기 어렵다. 그때는
  // 가로 드래그를 그대로 쓴다 — 화면에서 팔이 도는 방향과 손가락 방향이 맞는다.
  const screenAxis = new THREE.Vector2(b.x - a.x, -(b.y - a.y));
  const gain = 0.5; // 화면 100px ≈ 관절 50단위
  if (screenAxis.length() < 0.02) return dx * gain;
  const perpendicular = new THREE.Vector2(-screenAxis.y, screenAxis.x).normalize();
  return (dx * perpendicular.x + dy * perpendicular.y) * gain;
}

/** 리스를 막 잡았을 때는 첫 목표가 팔의 현재 자세 근처여야 한다.
 *
 * 서버가 그렇게 요구하는데(`POSE_NOT_SYNCED`), 그것을 모르고 멀리 던지면 명령이 조용히
 * 거절되고 팔은 꿈쩍도 하지 않는다. 실물에서 그 화면을 봤다. 거절당할 값을 보내는 대신
 * 갈 수 있는 데까지 보내고, 나머지는 다음 명령이 이어 간다. */
function clampToSyncWindow(item, value) {
  if (!lease?.needs_sync) return value;
  const present = presentOf(item.name);
  if (present === undefined) return value;
  const tolerance =
    (item.unit === 'percent'
      ? policy.sync_tolerance_percent ?? 10
      : policy.sync_tolerance_deg ?? 6) * 0.8;
  return Math.min(present + tolerance, Math.max(present - tolerance, value));
}

function presentOf(name) {
  return present[name];
}

function setTarget(name, value) {
  const item = specByName.get(name);
  if (!item) return;
  const wanted = Math.min(item.max, Math.max(item.min, value));
  target[name] = clampToSyncWindow(item, wanted);
  applyPose(ghost, target);
  paintJoints();
}

/** 조작을 끝낸다. 데드맨 — 손을 떼면 목표가 지금 자리로 붙어 팔이 그 자리에 선다. */
function releaseGrip() {
  grabbing = null;
  commanding = false;
  target = { ...present };
  applyPose(ghost, target);
  el('grabbed').hidden = true;
  toNative({ type: 'grab', joint: null });
  paintJoints();
}

const pointer = new THREE.Vector2();
const raycaster = new THREE.Raycaster();
let last = null;

function jointUnderPointer(event) {
  const rect = canvas.getBoundingClientRect();
  pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const hits = raycaster.intersectObject(live.stage, true);
  for (const hit of hits) {
    const link = hit.object.userData.link;
    const urdfJoint = link && live.linkToJoint.get(link);
    if (!urdfJoint) continue;
    const item = spec.find((s) => s.urdf_joint === urdfJoint);
    if (item) return item.name;
  }
  return null;
}

canvas.addEventListener('pointerdown', (event) => {
  if (!live || !canCommand()) return;
  const name = jointUnderPointer(event);
  if (!name) return;
  grabbing = name;
  commanding = true;
  last = { x: event.clientX, y: event.clientY };
  controls.enabled = false;
  canvas.setPointerCapture(event.pointerId);
  const item = specByName.get(name);
  el('grabbed').hidden = false;
  el('grabbed').innerHTML = `<b>${item.label}</b> 끌어서 움직이는 중 · 손을 떼면 멈춥니다`;
  toNative({ type: 'grab', joint: name });
});

canvas.addEventListener('pointermove', (event) => {
  if (!grabbing || !last) return;
  const dx = event.clientX - last.x;
  const dy = event.clientY - last.y;
  last = { x: event.clientX, y: event.clientY };
  setTarget(grabbing, (target[grabbing] ?? present[grabbing] ?? 0) + dragToDelta(grabbing, dx, dy));
});

for (const type of ['pointerup', 'pointercancel', 'pointerleave']) {
  canvas.addEventListener(type, () => {
    if (!grabbing) return;
    controls.enabled = true;
    releaseGrip();
  });
}

// 키보드. 숫자로 관절을 고르고 화살표로 움직인다. 누르고 있는 동안만 움직이는 것은
// 손가락과 같다 — 떼면 선다.
let selected = 0;
const held = new Set();
window.addEventListener('keydown', (event) => {
  if (event.target instanceof HTMLInputElement) return;
  if (event.key >= '1' && event.key <= '6') {
    selected = Number(event.key) - 1;
    paintJoints();
    return;
  }
  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight' || event.key === 'ArrowUp' || event.key === 'ArrowDown') {
    if (!canCommand()) return;
    event.preventDefault();
    held.add(event.key);
    commanding = true;
  }
  if (event.key === ' ' || event.key === 'Escape') {
    event.preventDefault();
    holdNow();
  }
});
window.addEventListener('keyup', (event) => {
  if (!held.delete(event.key)) return;
  if (held.size === 0) releaseGrip();
});
window.addEventListener('blur', () => {
  if (held.size || grabbing) {
    held.clear();
    releaseGrip();
  }
});

function stepFromKeys() {
  if (!held.size || !spec.length) return;
  const item = spec[Math.min(selected, spec.length - 1)];
  const cap = item.unit === 'percent' ? policy.step_percent ?? 3 : policy.step_deg ?? 2;
  let delta = 0;
  if (held.has('ArrowRight') || held.has('ArrowUp')) delta += cap;
  if (held.has('ArrowLeft') || held.has('ArrowDown')) delta -= cap;
  if (delta) setTarget(item.name, (target[item.name] ?? present[item.name] ?? 0) + delta);
}

// ---------------------------------------------------------------- 전송

let socket = null;
let socketTimer = null;

function canCommand() {
  if (HOST === 'native') return Boolean(nativeEnabled);
  return Boolean(lease) && telemetry && ['READY', 'ACTIVE'].includes(telemetry.state);
}

function openSocket() {
  if (HOST === 'native') return;
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  socket = new WebSocket(`${scheme}://${location.host}/api/vleader/stream`);
  socket.addEventListener('message', (event) => {
    const message = JSON.parse(event.data);
    if (message.type === 'hello') applyHello(message);
    else if (message.type === 'telemetry') applyTelemetry(message);
    else if (message.type === 'reject') showReject(message);
    else if (message.type === 'lease') lease = message;
  });
  socket.addEventListener('close', () => {
    socket = null;
    setStateLine('연결이 끊겼습니다 · 다시 붙는 중', 'stop');
    clearTimeout(socketTimer);
    socketTimer = setTimeout(openSocket, 1200);
  });
  socket.addEventListener('error', () => socket?.close());
}

function sendCommand() {
  if (!spec.length || !canCommand()) return;
  const joints = {};
  for (const item of spec) {
    const value = target[item.name];
    if (value !== undefined) joints[item.name] = Number(value.toFixed(3));
  }
  if (HOST === 'native') {
    toNative({ type: 'target', joints, commanding });
    return;
  }
  if (!socket || socket.readyState !== WebSocket.OPEN || !lease) return;
  socket.send(
    JSON.stringify({
      type: 'command',
      lease_id: lease.lease_id,
      session_id: SESSION,
      sequence: ++sequence,
      observation: telemetry?.observation,
      valid_for_ms: 300,
      joints,
    })
  );
}

/** 리스 하트비트.
 *
 * 명령과 따로 보낸다. PROTOCOL.md가 그렇게 하라고 적어 둔 이유가 여기서 그대로 드러났다 —
 * HOLD에 걸려 있는 동안에는 명령을 보내지 않으므로, 명령으로만 리스를 갱신하면 멈춰 서
 * 있는 5초 사이에 조작 권한이 남에게 넘어간다. 실제로 그렇게 넘어가는 것을 한 번 봤다.
 */
function beat() {
  if (!lease) return;
  if (HOST === 'native') {
    toNative({ type: 'heartbeat' });
    return;
  }
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: 'heartbeat', lease_id: lease.lease_id }));
  }
}

function holdNow() {
  if (HOST === 'native') {
    toNative({ type: 'hold' });
    return;
  }
  socket?.send(JSON.stringify({ type: 'hold' }));
  fetch('/api/vleader/hold', { method: 'POST' }).catch(() => {});
}

// ---------------------------------------------------------------- 화면

function setStateLine(text, kind = '') {
  const line = el('state-line');
  line.textContent = text;
  line.className = kind;
}

function showReject(message) {
  const banner = el('banner');
  banner.hidden = false;
  banner.classList.add('warn');
  el('banner-title').textContent = '거절됨';
  el('banner-detail').textContent = message.message || message.code;
  el('banner-resume').hidden = true;
  clearTimeout(showReject.timer);
  showReject.timer = setTimeout(() => {
    if (!telemetry?.fault) banner.hidden = true;
  }, 3500);
}

function applyHello(message) {
  policy = message.policy || {};
  spec = message.spec || [];
  specByName = new Map(spec.map((item) => [item.name, item]));
  buildJointRows();
  applyTelemetry(message);
}

function applyTelemetry(message) {
  telemetry = message;
  lease = message.lease && lease && message.lease.lease_id === lease.lease_id ? message.lease : lease;
  if (message.lease === null) lease = null;
  const values = {};
  for (const joint of message.joints || []) values[joint.name] = joint.present;
  present = values;
  applyPose(live, present);
  if (!framed && live) {
    framed = true;
    frameModel(live.stage);
  }
  if (!commanding) {
    target = { ...present };
    applyPose(ghost, target);
  }
  paintState();
  paintJoints();
}

function paintState() {
  if (!telemetry) return;
  // 루프가 돌지 않으면 우리가 그리는 자세는 실제 팔의 자세가 아니다. 그럴듯한 모델을
  // 그대로 두면 화면이 "팔이 이 자세다"라고 거짓말을 한다.
  document.body.classList.toggle('unknown-pose', telemetry.state === 'STOPPED');
  const fault = telemetry.fault;
  const banner = el('banner');
  if (fault) {
    banner.hidden = false;
    banner.classList.toggle('warn', telemetry.state !== 'FAULT');
    el('banner-title').textContent =
      telemetry.state === 'RETREATING' ? '물러나는 중' : '멈췄습니다';
    el('banner-detail').textContent = fault.message;
    el('banner-resume').hidden = telemetry.state === 'RETREATING';
  } else if (!showReject.timer) {
    banner.hidden = true;
  }
  const holder = telemetry.lease ? telemetry.lease.holder : null;
  const words = [telemetry.state_korean];
  if (telemetry.torque_enabled) words.push('토크 걸림');
  if (holder) words.push(`${holder} 조작 중`);
  setStateLine(
    words.join(' · '),
    telemetry.state === 'ACTIVE' ? 'live' : telemetry.state === 'HOLD' || telemetry.state === 'FAULT' ? 'stop' : 'warn'
  );
  el('lease-idle').hidden = Boolean(lease);
  el('lease-held').hidden = !lease;
  if (telemetry.lease) el('lease-holder').textContent = telemetry.lease.holder;
}

function buildJointRows() {
  const host = el('joints');
  host.innerHTML = '';
  for (const [index, item] of spec.entries()) {
    const row = document.createElement('div');
    row.className = 'joint';
    row.dataset.joint = item.name;
    row.innerHTML = `
      <label for="slider-${item.name}">${index + 1}. ${item.label}</label>
      <input id="slider-${item.name}" type="range" min="${item.min}" max="${item.max}" step="0.1" value="0">
      <output>0</output>
      <div class="load"><i></i></div>`;
    const slider = row.querySelector('input');
    slider.addEventListener('pointerdown', () => {
      commanding = canCommand();
    });
    slider.addEventListener('input', () => {
      if (!canCommand()) {
        paintJoints();
        return;
      }
      commanding = true;
      setTarget(item.name, Number(slider.value));
    });
    for (const type of ['pointerup', 'pointercancel']) {
      slider.addEventListener(type, releaseGrip);
    }
    host.append(row);
  }
}

function paintJoints() {
  for (const item of spec) {
    const row = document.querySelector(`.joint[data-joint="${item.name}"]`);
    if (!row) continue;
    const value = target[item.name] ?? present[item.name] ?? 0;
    const slider = row.querySelector('input');
    if (document.activeElement !== slider) slider.value = String(value);
    const reading = telemetry?.joints?.find((joint) => joint.name === item.name);
    // 목표와 실제가 벌어져 있으면 둘 다 적는다. 막혀서 선 팔에서는 이 차이가 곧 무슨 일이
    // 벌어졌는지에 대한 설명이고, 하나만 적으면 화면이 실제와 다른 말을 하게 된다.
    const unit = item.unit === 'percent' ? '%' : '°';
    const digits = item.unit === 'percent' ? 0 : 1;
    const measured = present[item.name];
    row.querySelector('output').textContent =
      measured !== undefined && Math.abs(measured - value) > 0.5
        ? `${value.toFixed(digits)} → ${measured.toFixed(digits)}${unit}`
        : `${value.toFixed(digits)}${unit}`;
    row.classList.toggle('limited', Boolean(reading?.rate_limited));
    const load = Math.min(1, Math.abs(reading?.load ?? 0) / (policy.load_trip || 400));
    row.querySelector('.load i').style.width = `${(load * 100).toFixed(0)}%`;
    row.classList.toggle('hot', load > 0.6);
    row.classList.toggle('tripped', telemetry?.fault?.joint === item.name);
  }
}

// ---------------------------------------------------------------- 권한

/** 서버가 요구하는 문구. 화면이 사람에게 요구하는 것과는 다르다.
 *
 * 조작 권한은 한 세션에 여러 번 받는다 — 잠깐 반납했다가 다시 잡고, 멈췄다가 다시
 * 시작한다. 그때마다 열세 글자를 치게 하면 게이트가 아니라 통행세가 되고, 통행세는 사람을
 * 신중하게 만들지 않는다. 대신 한 번의 분명한 행동을 요구한다 — 현장을 확인했다는 체크.
 *
 * 토크 해제는 그대로 옮겨 적게 둔다. 자주 하는 일이 아니고, 잘못 눌리면 팔이 떨어진다. */
const ARM_CONFIRMATION = 'MOVE SOARM101';

function refreshTakeButton() {
  const ok = el('confirm').checked && el('token').value.trim().length > 0;
  el('take').disabled = !ok;
  el('release-go').disabled = el('release-confirm').value.trim() !== 'RELEASE TORQUE SOARM101';
}
['token', 'release-confirm'].forEach((id) => el(id).addEventListener('input', refreshTakeButton));
el('confirm').addEventListener('change', refreshTakeButton);

const savedToken = localStorage.getItem('soarm-motion-token');
if (savedToken) el('token').value = savedToken;

async function post(path, body) {
  const response = await fetch(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-SOARM-Motion-Token': el('token').value.trim(),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
  return payload;
}

el('take').addEventListener('click', async () => {
  el('take').disabled = true;
  try {
    localStorage.setItem('soarm-motion-token', el('token').value.trim());
    const phrase = ARM_CONFIRMATION;
    if (!telemetry?.running) await post('/api/vleader/start');
    if (!telemetry?.torque_enabled) {
      await post('/api/vleader/arm', { confirmation: phrase });
    }
    // 리스에도 같은 문구가 붙는다. 토크가 이미 걸려 있으면 위 줄을 지나치기 때문이다.
    lease = await post('/api/vleader/lease', {
      confirmation: phrase, holder: HOLDER, session_id: SESSION,
    });
    // 확인은 한 번 쓰고 내린다. 다음 사람이 그대로 눌러 시작하지 못하게 한다.
    el('confirm').checked = false;
    target = { ...present };
    paintState();
  } catch (error) {
    showReject({ message: String(error.message || error) });
  }
  refreshTakeButton();
});

el('give-back').addEventListener('click', () => giveBack());

el('hold').addEventListener('click', holdNow);
el('banner-resume').addEventListener('click', async () => {
  try {
    await post('/api/vleader/resume');
    target = { ...present };
  } catch (error) {
    showReject({ message: String(error.message || error) });
  }
});
el('release-torque').addEventListener('click', () => el('torque-dialog').showModal());
el('torque-dialog').addEventListener('close', async () => {
  if (el('torque-dialog').returnValue !== 'release') return;
  try {
    await post('/api/vleader/torque/release', { confirmation: el('release-confirm').value.trim() });
  } catch (error) {
    showReject({ message: String(error.message || error) });
  }
  el('release-confirm').value = '';
});

/** 조작 권한을 돌려준다. 팔은 지금 자리에 선다 — 떨어뜨리지 않는다. */
function giveBack(keepalive = false) {
  const held = lease;
  lease = null;
  paintState();
  if (!held) return;
  fetch(`/api/vleader/lease/${held.lease_id}`, {
    method: 'DELETE',
    headers: { 'X-SOARM-Motion-Token': el('token').value.trim() },
    keepalive,
  }).catch(() => {});
}

// 화면을 떠나면 조작 권한을 놓는다.
//
// 만료를 기다리지 않고 명시적으로 반납하는 이유는, 기다리는 5초 동안 다른 기기가 조작을
// 시작할 수 없기 때문이다. 그 5초는 팔이 안전해지는 시간이 아니라 아무도 못 만지는 시간이다.
window.addEventListener('pagehide', () => {
  releaseGrip();
  giveBack(true);
  socket?.close();
});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) return;
  releaseGrip();
  giveBack();
});

// ---------------------------------------------------------------- 카메라

function startCameras() {
  if (HOST === 'native') return;
  for (const figure of document.querySelectorAll('.camera')) {
    const image = figure.querySelector('img');
    image.src = `/api/cameras/${figure.dataset.camera}.mjpg?v=${Date.now()}`;
    image.addEventListener('load', () => image.classList.remove('down'));
    image.addEventListener('error', () => {
      // 깨진 이미지 아이콘을 남겨 두지 않는다. 카메라가 없는 것과 화면이 고장 난 것은
      // 다른 일인데, 그 아이콘은 둘을 같아 보이게 한다.
      image.classList.add('down');
      setTimeout(() => {
        image.src = `/api/cameras/${figure.dataset.camera}.mjpg?v=${Date.now()}`;
      }, 3000);
    });
  }
}

// ---------------------------------------------------------------- 네이티브 다리

let nativeEnabled = false;
window.soarmViewer = {
  /** 앱이 서버에서 받은 관절 계약을 넘겨준다. */
  spec(payload) {
    applyHello(payload);
  },
  /** 앱이 받은 텔레메트리 한 장. */
  telemetry(payload) {
    applyTelemetry(payload);
  },
  /** 조작 가능한 상태인가(리스를 앱이 쥐고 있는가). */
  setEnabled(value) {
    nativeEnabled = Boolean(value);
    if (!nativeEnabled) releaseGrip();
  },
  /** 앱의 슬라이더가 움직였다. */
  setTarget(name, value) {
    commanding = true;
    setTarget(name, value);
  },
  endTarget() {
    releaseGrip();
  },
  /** 한 장 그린다.
   *
   * 평소에는 `requestAnimationFrame`이 알아서 돌지만, 창이 화면에 없거나 탭이 뒤에
   * 있으면 브라우저가 그 시계를 멈춘다. 그때도 마지막 상태를 한 장 그려야 하는 경우가
   * 있어서(맥 앱이 창을 처음 띄우는 순간, 그리고 화면을 찍어 확인할 때) 밖에서 부를 수
   * 있는 자리를 하나 둔다. */
  render() {
    controls.update();
    renderer.render(scene, camera);
    return renderer.info.render.triangles;
  },
};

// ---------------------------------------------------------------- 시작

let lastSend = 0;
// 하트비트는 애니메이션 프레임이 아니라 타이머로 보낸다. 탭이 뒤로 가면 브라우저가
// `requestAnimationFrame`을 멈추는데, 그러면 화면이 살아 있는지와 무관하게 리스가
// 만료된다. 뒤로 간 탭이 권한을 놓는 것은 맞지만, 그 결정은 아래 `visibilitychange`가
// 명시적으로 해야 한다 — 프레임 스케줄러의 부작용으로 일어날 일이 아니다.
setInterval(beat, 800);
function frame(now) {
  requestAnimationFrame(frame);
  stepFromKeys();
  controls.update();
  renderer.render(scene, camera);
  if (now - lastSend >= 1000 / COMMAND_HZ) {
    lastSend = now;
    sendCommand();
  }
}

async function boot() {
  try {
    live = await loadURDF('/static/viewer/urdf/so101.urdf', '/static/viewer/urdf/');
    scene.add(live.stage);
    ghost = await loadURDF('/static/viewer/urdf/so101.urdf', '/static/viewer/urdf/');
    ghost.stage.traverse((object) => {
      if (!object.isMesh) return;
      object.material = object.material.clone();
      object.material.transparent = true;
      object.material.opacity = 0.22;
      object.material.depthWrite = false;
      object.material.color = new THREE.Color(0x5f82ff);
    });
    scene.add(ghost.stage);
    document.body.classList.remove('loading');
    resize();
    applyPose(live, Object.fromEntries(spec.map((item) => [item.name, 0])));
    frameModel(live.stage);
    if (HOST === 'web') {
      const payload = await (await fetch('/api/vleader')).json();
      applyHello(payload);
      openSocket();
      startCameras();
    } else {
      const context = renderer.getContext();
      const info = context && context.getExtension('WEBGL_debug_renderer_info');
      toNative({
        type: 'ready',
        renderer: info ? context.getParameter(info.UNMASKED_RENDERER_WEBGL) : 'WebGL',
        triangles: renderer.info.render.triangles,
      });
    }
    requestAnimationFrame(frame);
  } catch (error) {
    const message = `3D를 열지 못했습니다: ${error.message || error}`;
    el('boot-detail').textContent = message;
    toNative({ type: 'error', message });
    console.error(error);
  }
}

// 페이지 안에서 난 사고를 품고 있지 않는다. 맥 앱은 이 페이지를 창 안에 넣어 두고
// 콘솔을 볼 수 없으므로, 여기서 말해 주지 않으면 남는 것은 검은 사각형뿐이다.
window.addEventListener('error', (event) => {
  toNative({ type: 'error', message: String(event.message || event.error || 'unknown') });
});
window.addEventListener('unhandledrejection', (event) => {
  toNative({ type: 'error', message: String(event.reason?.message || event.reason || 'unknown') });
});

boot();
