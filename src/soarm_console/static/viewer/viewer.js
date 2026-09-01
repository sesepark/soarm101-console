// SO-ARM101 가상 리더 뷰어.
//
// 이 파일 하나가 두 기기를 모두 그린다.
//
// - `?host=web`  — 아이폰과 브라우저. 이 페이지가 WebSocket을 직접 열고, 조작 UI·카메라·
//                  정지 버튼·권한까지 자기 화면에 그린다.
// - `?host=native` — 맥. 3D만 그리고 나머지는 네이티브 SwiftUI가 맡는다. 전송도 앱이
//                  하고, 이 페이지는 `window.webkit.messageHandlers.soarm`으로 목표를
//                  올리고 `window.soarmViewer.*`로 상태를 받아 그린다.
//
// 3D를 두 번 만들지 않는 것이 요점이다. 구현이 둘이면 두 기기가 같은 팔에 대해 서로 다른
// 그림을 그리게 되고, 그 차이는 조작하는 순간에만 드러난다.
//
// ## 조작 방식이 둘이다
//
// - `관절` — 관절 하나를 골라 그 각도를 정한다. 3D에서 링크를 집어 끌거나 슬라이더로.
//   화면은 손가락으로 돌릴 수 있다.
// - `끝점` — 집게 끝을 잡아 원하는 자리로 끈다. 어깨 회전·어깨 들기·팔꿈치를 역기구학이
//   함께 푼다. 이때 **화면은 고정된다** — 끌면 시점이 따라 돌아 버리면 끝점을 어디로
//   보내는지 알 수 없기 때문이고, 시점은 ⟲ ⟳로 90°씩만 돌린다.
//
// 둘 다 결과는 같은 것 하나다: **관절 여섯 개의 절대 목표.** 서버로 나가는 것은 그것뿐이고,
// 안전 사다리도 그 하나만 심사한다. 조작 방식을 늘려도 팔이 받는 명령의 종류는 늘지 않는다.
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
/** 역기구학이 푸는 관절. 나머지(손목 둘과 집게)는 사람이 직접 정한다.
 *
 *  셋으로 3차원 위치를 맞추면 답이 하나로 정해진다. 손목까지 넣으면 남는 자유도가
 *  생기고, 그러면 같은 자리를 여러 자세로 갈 수 있어 손을 뗄 때마다 팔이 다른 모양이
 *  된다. 손목을 사람이 쥐고 있으면 그 대신 **손목 각도를 바꿔도 끝점이 제자리에 남는다** —
 *  역기구학이 나머지 셋으로 따라오기 때문이고, 실제로 그쪽이 더 쓸모 있다. */
const IK_JOINTS = ['shoulder_pan', 'shoulder_lift', 'elbow_flex'];
/** 끝점 모드에서 사람이 직접 정하는 관절. */
const HAND_JOINTS = ['wrist_flex', 'wrist_roll', 'gripper'];
/** URDF에서 집게 끝을 나타내는 링크. */
const TCP_LINK = 'gripper_frame_link';

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

scene.add(new THREE.HemisphereLight(0xdfe7ff, 0x0b1018, 1.15));
const key = new THREE.DirectionalLight(0xffffff, 1.5);
key.position.set(0.6, 1.0, 0.5);
scene.add(key);
const grid = new THREE.GridHelper(1.2, 12, 0x2a3550, 0x161d2c);
grid.material.transparent = true;
grid.material.opacity = 0.6;
scene.add(grid);

/** 끝점 모드에서 지금 끌고 있는 자리. 실제 집게 끝은 여기로 수렴한다. */
const pin = new THREE.Mesh(
  new THREE.SphereGeometry(0.011, 20, 14),
  new THREE.MeshBasicMaterial({ color: 0x5f82ff, transparent: true, opacity: 0.85 })
);
pin.visible = false;
scene.add(pin);

/** 모델을 담는 상자. 시점을 다시 잡을 때마다 쓰므로 한 번 재어 둔다. */
let frame = { center: new THREE.Vector3(0, 0.12, 0), radius: 0.3, distance: 0.7 };
let framed = false;

function measureModel(object) {
  const box = new THREE.Box3().setFromObject(object);
  if (box.isEmpty()) return;
  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  // 팔은 밑동에서 **위로만** 뻗는다. 상자 한가운데를 겨누면 그린 팔이 화면 중단~상단에
  // 몰리고 아래쪽에는 빈 격자만 남았다. 겨누는 점을 조금 올려 팔을 화면 가운데로 내린다.
  center.y += size.y * 0.12;

  // 화면의 **가로세로 둘 다**에 맞춘다.
  //
  // 처음에는 상자를 감싸는 구의 반지름 하나로 거리를 정했다. 팔처럼 길쭉한 물체에서는
  // 그 구가 실제보다 한참 커서, 넓고 낮은 캔버스(데스크톱의 3D 칸이 그렇다)에서 팔이
  // 화면 한가운데 작게 떠 있었다. 세로는 세로 화각으로, 가로는 화면 비율에서 나오는
  // 가로 화각으로 각각 재고 먼 쪽을 쓴다.
  const vertical = (camera.fov * Math.PI) / 360;
  const aspect = Math.max(0.2, camera.aspect || 1);
  const horizontal = Math.atan(Math.tan(vertical) * aspect);
  const spread = Math.hypot(size.x, size.z);
  const distance = Math.max(
    size.y / 2 / Math.tan(vertical),
    spread / 2 / Math.tan(horizontal)
  ) * 1.35;
  const radius = size.length() / 2;
  frame = { center, radius, distance };
  controls.minDistance = radius * 0.5;
  controls.maxDistance = distance * 3;
  camera.near = Math.max(0.005, distance / 200);
  camera.far = distance * 20;
  camera.updateProjectionMatrix();
  grid.position.y = box.min.y;
}

/** 고정 시점 넷. 끝점 모드에서 화면이 돌지 않게 하려면 어디를 보는지 정해 두어야 한다. */
const VIEWS = [
  { name: '앞', azimuth: 0 },
  { name: '오른쪽', azimuth: Math.PI / 2 },
  { name: '뒤', azimuth: Math.PI },
  { name: '왼쪽', azimuth: -Math.PI / 2 },
];
let viewIndex = 0;

function applyFixedView() {
  const { azimuth } = VIEWS[viewIndex];
  const elevation = 0.42; // 라디안. 조금 위에서 내려다보아야 책상면과 팔이 함께 읽힌다.
  const d = frame.distance;
  camera.position.set(
    frame.center.x + Math.sin(azimuth) * Math.cos(elevation) * d,
    frame.center.y + Math.sin(elevation) * d,
    frame.center.z + Math.cos(azimuth) * Math.cos(elevation) * d
  );
  controls.target.copy(frame.center);
  camera.lookAt(frame.center);
  controls.update();
  el('view-name').textContent = VIEWS[viewIndex].name;
}

function applyFreeView() {
  controls.target.copy(frame.center);
  camera.position
    .copy(frame.center)
    .add(new THREE.Vector3(0.62, 0.5, 0.62).normalize().multiplyScalar(frame.distance));
  controls.update();
}

let lastAspect = 0;
function resize() {
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  renderer.setSize(rect.width, rect.height, false);
  const aspect = rect.width / rect.height;
  camera.aspect = aspect;
  camera.updateProjectionMatrix();
  // 칸의 모양이 크게 달라졌으면 다시 잡는다. 폰을 눕히거나, 탭을 바꾸거나, 창을
  // 키우면 같은 거리가 더 이상 맞지 않는다 — 한 번 잡고 두면 팔이 화면 밖으로
  // 밀리거나 손톱만 하게 남는다.
  if (framed && live && (lastAspect === 0 || Math.abs(aspect - lastAspect) / aspect > 0.15)) {
    measureModel(live.stage);
    if (mode === 'endpoint') applyFixedView();
    else {
      // 자유 시점에서는 **보고 있던 방향을 지킨다.** 창 크기를 바꿨다고 시점이
      // 처음으로 되돌아가면, 애써 맞춰 둔 각도를 매번 다시 만들어야 한다.
      const direction = camera.position.clone().sub(controls.target).normalize();
      controls.target.copy(frame.center);
      camera.position.copy(frame.center).add(direction.multiplyScalar(frame.distance));
      controls.update();
    }
  }
  lastAspect = aspect;
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
let profiles = [];
let profileName = null;
let live = null; // 로봇 모델(실측)
let ghost = null; // 목표 모델(반투명)
let grabbing = null; // 지금 끌고 있는 관절 이름
let commanding = false;
let sequence = 0;
let mode = localStorage.getItem('soarm-mode') === 'endpoint' ? 'endpoint' : 'joint';
let tab = 'drive';
let selectedCamera = localStorage.getItem('soarm-camera') === 'wrist' ? 'wrist' : 'scene';

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
  el('boot-detail').textContent =
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

function presentOf(name) {
  return present[name];
}

/** 리스를 막 잡았을 때는 첫 목표가 팔의 현재 자세 근처여야 한다.
 *
 * 서버가 그렇게 요구하는데(`POSE_NOT_SYNCED`), 그것을 모르고 멀리 던지면 명령이 조용히
 * 거절되고 팔은 꿈쩍도 하지 않는다. 실물에서 그 화면을 봤다. 거절당할 값을 보내는 대신
 * 갈 수 있는 데까지 보내고, 나머지는 다음 명령이 이어 간다. */
function clampToSyncWindow(item, value) {
  if (!lease?.needs_sync) return value;
  const here = presentOf(item.name);
  if (here === undefined) return value;
  const tolerance =
    (item.unit === 'percent'
      ? policy.sync_tolerance_percent ?? 15
      : policy.sync_tolerance_deg ?? 10) * 0.8;
  return Math.min(here + tolerance, Math.max(here - tolerance, value));
}

function setTarget(name, value) {
  const item = specByName.get(name);
  if (!item) return;
  const wanted = Math.min(item.max, Math.max(item.min, value));
  target[name] = clampToSyncWindow(item, wanted);
  applyPose(ghost, target);
  paintControls();
}

/** 조작을 끝낸다. 데드맨 — 손을 떼면 목표가 지금 자리로 붙어 팔이 그 자리에 선다. */
function releaseGrip() {
  grabbing = null;
  commanding = false;
  dragging = null;
  lastEndpointAt = 0;
  target = { ...present };
  applyPose(ghost, target);
  pin.visible = false;
  el('grabbed').hidden = true;
  el('reach-note').hidden = true;
  toNative({ type: 'grab', joint: null });
  paintControls();
}

// ---------------------------------------------------------------- 역기구학
//
// 수치 야코비안 + 감쇠 최소자승(DLS). 팔이 세 관절뿐이라 3×3이고, 손으로 풀어 쓸 수 있다.
//
// 닫힌 해를 쓰지 않는 이유가 있다. 닫힌 해는 URDF의 링크 길이와 축 방향을 코드 안에 다시
// 적어야 하는데, 그 순간 화면이 그리는 팔과 계산이 믿는 팔이 갈라진다 — URDF를 바꾸면
// 한쪽만 따라온다. 여기서는 **화면이 쓰는 바로 그 순기구학**을 그대로 미분한다. 느리지만
// (한 프레임에 3~4번 반복) 손가락 속도에는 충분하고, 팔이 바뀌어도 저절로 맞는다.
//
// 감쇠를 두는 이유는 특이점이다. 팔을 곧게 펴면 야코비안이 얇아져 역행렬이 폭발하고,
// 그때 감쇠가 없으면 손가락을 1mm 움직였는데 관절이 90° 돌아간다.

/** 감쇠의 **비율**. 절대값이 아니다.
 *
 * 처음에는 λ² = 0.02라는 상수를 그대로 썼다가 팔이 꿈쩍도 하지 않았다. 야코비안의 단위가
 * **미터/도**라서 성분이 0.001~0.005쯤이고, 그래서 JJᵀ의 성분은 1e-5 언저리다. 거기에
 * 0.02를 더하면 원래 값이 통째로 묻힌다 — 손가락으로 12px을 끌었을 때 팔이 0.3mm 움직였고,
 * 계산은 "맞게" 돌고 있었으므로 화면에는 아무 오류도 뜨지 않았다.
 *
 * 그래서 감쇠를 **JJᵀ 자신의 크기에 비례**해 정한다(Levenberg–Marquardt가 하는 방식이다).
 * 단위가 바뀌어도, 팔의 크기가 바뀌어도 같은 뜻으로 남는다. */
const IK_DAMPING_RATIO = 0.02;
const IK_MAX_STEP = 6; // 반복 한 번에 움직이는 관절 각도의 상한(도).
/** 목표가 **팔이 실제로 갈 수 있는 속도보다 빨리** 앞서 나가지 않게 한다.
 *
 * 끝점 모드에는 관절 모드에 없는 성질이 하나 있다: 화면에서 조금 끈 것이 관절에서는 클
 * 수 있다. 팔이 거의 펴진 자세에서는 손끝을 옆으로 2cm 옮기는 데 어깨가 크게 돌아야 하고,
 * 실측(2026-09-02)에서 18px 드래그 한 번이 `shoulder_lift`를 23° 돌렸다.
 *
 * 팔이 튀지는 않는다 — 속도는 서보가 지키고 목표는 절대값이라 천천히 수렴한다. 문제는
 * **화면**이다. 유령(목표)만 저 앞에 가 있고 팔은 뒤에서 따라오면, 손가락과 팔이 서로
 * 다른 것을 하는 것처럼 보이고 어디서 손을 떼야 할지 알 수 없다.
 *
 * 그래서 목표의 속도를 서보의 속도 상한에 맞춘다. 방향은 그대로 두고 크기만 줄이므로
 * 계속 끌면 팔은 손가락을 따라 이어서 가고, 유령은 팔보다 한 발짝 앞에만 있는다. */
function maxJointStep(seconds) {
  const perSecond = policy.max_deg_per_s ?? 90;
  // 한 프레임이 오래 걸렸다고 그만큼 크게 뛰지는 않는다. 창을 뒤로 보냈다 돌아온 순간이
  // 그런 자리다.
  return Math.max(0.4, perSecond * Math.min(0.2, Math.max(0.008, seconds)));
}
let lastEndpointAt = 0;
const scratch = new THREE.Vector3();

function tcpPosition(model) {
  const link = model.stage.getObjectByName(TCP_LINK);
  if (!link) return null;
  model.stage.updateMatrixWorld(true);
  return scratch.setFromMatrixPosition(link.matrixWorld).clone();
}

/** 관절값 한 벌을 넣었을 때 집게 끝이 어디인가. `ghost`를 계산용으로 빌려 쓴다. */
function forward(values) {
  applyPose(ghost, values);
  return tcpPosition(ghost);
}

/**
 * `point`에 집게 끝이 오도록 세 관절을 푼다. 풀린 값을 돌려주고, 못 닿으면 `null`.
 *
 * 지금 자세에서 출발해 조금씩 다가간다. 그래서 답이 여럿인 자리에서도 **지금 자세에
 * 가장 가까운 답**이 나오고, 손가락을 끄는 동안 팔이 갑자기 뒤집히지 않는다.
 */
function solveIK(point, from) {
  const q = IK_JOINTS.map((name) => from[name] ?? present[name] ?? 0);
  const values = { ...from };
  let best = null;
  for (let iteration = 0; iteration < 12; iteration += 1) {
    IK_JOINTS.forEach((name, index) => (values[name] = q[index]));
    const here = forward(values);
    if (!here) return null;
    const error = point.clone().sub(here);
    const distance = error.length();
    if (best === null || distance < best.distance) {
      best = { distance, values: { ...values } };
    }
    if (distance < 0.0008) break;

    // 수치 야코비안. 관절 하나를 조금 돌려 끝이 얼마나 움직이는지 본다.
    const delta = 0.35; // 도
    const columns = [];
    for (let j = 0; j < IK_JOINTS.length; j += 1) {
      const probe = { ...values };
      probe[IK_JOINTS[j]] = q[j] + delta;
      const moved = forward(probe);
      if (!moved) return null;
      columns.push(moved.sub(here).divideScalar(delta));
    }
    // J J^T (3×3). 열 벡터 세 개로 만든다.
    const a = [];
    for (let r = 0; r < 3; r += 1) {
      a.push([]);
      for (let c = 0; c < 3; c += 1) {
        let sum = 0;
        for (let j = 0; j < 3; j += 1) {
          sum += columns[j].getComponent(r) * columns[j].getComponent(c);
        }
        a[r].push(sum);
      }
    }
    // 감쇠는 그 행렬 자신의 크기에서 나온다. 대각합의 평균에 비례해 두면 특이점에서만
    // 실제로 눌리고, 평소에는 거의 아무 일도 하지 않는다.
    const damping = Math.max(1e-12, ((a[0][0] + a[1][1] + a[2][2]) / 3) * IK_DAMPING_RATIO);
    for (let r = 0; r < 3; r += 1) a[r][r] += damping;
    const y = solve3(a, [error.x, error.y, error.z]);
    if (!y) break;
    for (let j = 0; j < 3; j += 1) {
      const step = columns[j].x * y[0] + columns[j].y * y[1] + columns[j].z * y[2];
      q[j] += Math.max(-IK_MAX_STEP, Math.min(IK_MAX_STEP, step));
      const item = specByName.get(IK_JOINTS[j]);
      if (item) q[j] = Math.min(item.max, Math.max(item.min, q[j]));
    }
  }
  IK_JOINTS.forEach((name, index) => (values[name] = q[index]));
  const reached = forward(values);
  const gap = reached ? reached.distanceTo(point) : Infinity;
  return { values, gap, best };
}

/** 3×3 선형계. 부분 피벗을 쓰는 가우스 소거 — 특이점 근처에서 0으로 나누지 않기 위해서다. */
function solve3(a, b) {
  const m = [
    [a[0][0], a[0][1], a[0][2], b[0]],
    [a[1][0], a[1][1], a[1][2], b[1]],
    [a[2][0], a[2][1], a[2][2], b[2]],
  ];
  for (let i = 0; i < 3; i += 1) {
    let pivot = i;
    for (let r = i + 1; r < 3; r += 1) {
      if (Math.abs(m[r][i]) > Math.abs(m[pivot][i])) pivot = r;
    }
    if (Math.abs(m[pivot][i]) < 1e-12) return null;
    [m[i], m[pivot]] = [m[pivot], m[i]];
    for (let r = 0; r < 3; r += 1) {
      if (r === i) continue;
      const factor = m[r][i] / m[i][i];
      for (let c = i; c < 4; c += 1) m[r][c] -= factor * m[i][c];
    }
  }
  return [m[0][3] / m[0][0], m[1][3] / m[1][1], m[2][3] / m[2][2]];
}

// ---------------------------------------------------------------- 손가락

/** 관절 모드에서 화면 위 드래그를 관절 각도로. 관절의 회전축을 화면에 투영해 그 수직
 *  성분만 쓴다. */
function dragToDelta(jointName, dx, dy) {
  const joint = live.joints.get(specByName.get(jointName)?.urdf_joint);
  if (!joint) return 0;
  const origin = new THREE.Vector3().setFromMatrixPosition(joint.pivot.matrixWorld);
  const axis = joint.axis.clone().transformDirection(joint.pivot.matrixWorld).normalize();
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

const pointer = new THREE.Vector2();
const raycaster = new THREE.Raycaster();
let last = null;
let dragging = null; // 'joint' | 'endpoint'
let ikPoint = null;

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
  if (mode === 'endpoint') {
    ikPoint = forward(target) || tcpPosition(live);
    if (!ikPoint) return;
    dragging = 'endpoint';
    commanding = true;
    last = { x: event.clientX, y: event.clientY };
    canvas.setPointerCapture(event.pointerId);
    pin.position.copy(ikPoint);
    pin.visible = true;
    el('grabbed').hidden = false;
    el('grabbed').innerHTML = '<b>집게 끝</b>을 끄는 중 · 손을 떼면 멈춥니다';
    toNative({ type: 'grab', joint: 'endpoint' });
    return;
  }
  const name = jointUnderPointer(event);
  if (!name) return;
  grabbing = name;
  dragging = 'joint';
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
  if (!dragging || !last) return;
  const dx = event.clientX - last.x;
  const dy = event.clientY - last.y;
  last = { x: event.clientX, y: event.clientY };
  if (dragging === 'joint') {
    setTarget(grabbing, (target[grabbing] ?? present[grabbing] ?? 0) + dragToDelta(grabbing, dx, dy));
    return;
  }
  // 끝점. 화면 평면 위에서 정확히 손가락만큼 옮긴다 — 끌고 있는 점을 화면 좌표로 바꾸고,
  // 그 자리에서 손가락이 간 만큼 더한 뒤 되돌린다. 원근이 있어도 손가락과 점이 어긋나지
  // 않는 것이 이 방식의 요점이다.
  const rect = canvas.getBoundingClientRect();
  const ndc = ikPoint.clone().project(camera);
  ndc.x += (2 * dx) / rect.width;
  ndc.y -= (2 * dy) / rect.height;
  moveEndpointTo(ndc.unproject(camera));
});

/** 끝점을 이 세계 좌표로 보낸다. 닿지 않으면 갈 수 있는 데까지만 간다. */
function moveEndpointTo(point) {
  const answer = solveIK(point, target);
  if (!answer) return;
  const reachable = answer.gap < 0.02 ? answer.values : answer.best?.values;
  if (!reachable) return;
  // 한 번에 도는 각도를 묶는다. 방향은 그대로 두고 크기만 줄이므로, 계속 끌면 팔은
  // 손가락을 따라 이어서 간다 — 목표가 절대값이라 중간에 끊겨도 어긋나지 않는다.
  const now = performance.now();
  const cap = maxJointStep(lastEndpointAt ? (now - lastEndpointAt) / 1000 : 0.033);
  lastEndpointAt = now;
  let largest = 0;
  for (const name of IK_JOINTS) {
    largest = Math.max(largest, Math.abs(reachable[name] - (target[name] ?? present[name] ?? 0)));
  }
  const scale = largest > cap ? cap / largest : 1;
  for (const name of IK_JOINTS) {
    const from = target[name] ?? present[name] ?? 0;
    setTarget(name, from + (reachable[name] - from) * scale);
  }
  // 닿지 못한 만큼은 끌고 있는 점을 되돌려 놓는다. 그러지 않으면 점만 손가락을 따라
  // 계속 도망가고, 손을 놓았을 때 팔은 엉뚱한 자리에 있다.
  const settled = forward(target);
  ikPoint = settled ? settled.clone() : point.clone();
  pin.position.copy(ikPoint);
  el('reach-note').hidden = answer.gap < 0.02;
}

for (const type of ['pointerup', 'pointercancel', 'pointerleave']) {
  canvas.addEventListener(type, () => {
    if (!dragging) return;
    controls.enabled = mode === 'joint';
    releaseGrip();
  });
}

// 앞뒤. 화면 평면만으로는 깊이를 정할 수 없다. 누르고 있는 동안만 움직이고 놓으면
// 가운데로 돌아오는 손잡이 — 손가락과 같은 성질이라 데드맨이 그대로 통한다.
let depthHeld = 0;
{
  const slider = el('depth');
  const reset = () => {
    depthHeld = 0;
    slider.value = '0';
    el('depth-out').textContent = '0';
    if (commanding && !dragging) releaseGrip();
  };
  slider.addEventListener('input', () => {
    if (!canCommand()) {
      reset();
      return;
    }
    depthHeld = Number(slider.value);
    el('depth-out').textContent = depthHeld === 0 ? '0' : depthHeld > 0 ? '앞' : '뒤';
    commanding = true;
  });
  for (const type of ['pointerup', 'pointercancel', 'keyup', 'blur']) {
    slider.addEventListener(type, reset);
  }
}

function stepDepth() {
  if (!depthHeld || mode !== 'endpoint' || !canCommand()) return;
  const here = forward(target);
  if (!here) return;
  const forwardAxis = new THREE.Vector3();
  camera.getWorldDirection(forwardAxis);
  // 위아래 성분은 뺀다. 앞뒤 손잡이가 높이까지 바꾸면 화면에서 무슨 일이 일어나는지
  // 읽을 수 없다 — 높이는 손가락이 정한다.
  forwardAxis.y = 0;
  if (forwardAxis.lengthSq() < 1e-6) return;
  forwardAxis.normalize().multiplyScalar(depthHeld * 0.0045);
  moveEndpointTo(here.add(forwardAxis));
}

// 키보드. 숫자로 관절을 고르고 화살표로 움직인다. 누르고 있는 동안만 움직이는 것은
// 손가락과 같다 — 떼면 선다.
let selected = 0;
const held = new Set();
window.addEventListener('keydown', (event) => {
  if (event.target instanceof HTMLInputElement) return;
  if (event.key >= '1' && event.key <= '6') {
    selected = Number(event.key) - 1;
    paintControls();
    return;
  }
  if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) {
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
  if (held.size || grabbing || dragging) {
    held.clear();
    releaseGrip();
  }
});

function stepFromKeys() {
  if (!held.size || !spec.length) return;
  const item = spec[Math.min(selected, spec.length - 1)];
  const cap = (item.unit === 'percent' ? policy.lead_percent ?? 12 : policy.lead_deg ?? 12) * 0.25;
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

/** 리스 하트비트. 명령과 따로 보낸다 — HOLD에 걸려 있는 동안에는 명령을 보내지 않으므로,
 *  명령으로만 갱신하면 멈춰 서 있는 사이에 조작 권한이 남에게 넘어간다. */
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

/** 이 페이지가 배너를 그려도 되는가. `host=native`에서는 맥 앱이 같은 배너를 자기 화면에
 *  그린다. 둘 다 그리면 "멈췄습니다"가 두 번 뜨고 `확인하고 계속`도 두 개가 된다. */
const OWNS_BANNER = HOST !== 'native';

function showReject(message) {
  if (!OWNS_BANNER) return;
  const banner = el('banner');
  banner.hidden = false;
  banner.classList.add('warn');
  el('banner-title').textContent = '거절됨';
  el('banner-detail').textContent = korean(message.message || message.code);
  el('banner-resume').hidden = true;
  clearTimeout(showReject.timer);
  showReject.timer = setTimeout(() => {
    showReject.timer = null;
    if (!telemetry?.fault) banner.hidden = true;
  }, 3500);
}

function applyHello(message) {
  policy = message.policy || {};
  spec = message.spec || [];
  specByName = new Map(spec.map((item) => [item.name, item]));
  buildControls();
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
    measureModel(live.stage);
    if (mode === 'endpoint') applyFixedView();
    else applyFreeView();
  }
  // 서버가 팔을 세웠으면 잡고 있던 손도 놓는다. 놓지 않으면 그리던 목표가 그대로 남고,
  // 다시 시작하는 순간 같은 곳으로 다시 밀어 붙어 또 멈춘다.
  if (commanding && ['HOLD', 'FAULT', 'RETREATING'].includes(telemetry.state)) {
    releaseGrip();
  }
  if (!commanding) {
    target = { ...present };
    applyPose(ghost, target);
  }
  paintState();
  paintControls();
}

function paintState() {
  if (!telemetry) return;
  document.body.classList.toggle('unknown-pose', telemetry.state === 'STOPPED');
  const fault = telemetry.fault;
  const banner = el('banner');
  if (!OWNS_BANNER) {
    banner.hidden = true;
  } else if (fault) {
    banner.hidden = false;
    banner.classList.toggle('warn', telemetry.state !== 'FAULT');
    el('banner-title').textContent =
      telemetry.state === 'RETREATING' ? '물러나는 중' : '멈췄습니다';
    el('banner-detail').textContent = fault.message;
    el('banner-resume').hidden = telemetry.state === 'RETREATING';
  } else if (!showReject.timer) {
    banner.hidden = true;
  }

  const words = [telemetry.state_korean];
  // 명령이 잠깐 끊겨 선 것은 HOLD와 다르다. 확인을 요구하지 않고, 명령이 다시 오면
  // 그대로 이어진다. 화면이 둘을 같게 말하면 사람은 멀쩡한 상태에서 버튼을 찾는다.
  if (telemetry.command_stalled) words.push('명령 끊김 · 대기');
  setStateLine(
    words.join(' · '),
    telemetry.state === 'ACTIVE' && !telemetry.command_stalled
      ? 'live'
      : ['HOLD', 'FAULT'].includes(telemetry.state)
        ? 'stop'
        : 'warn'
  );
  const holder = telemetry.lease ? telemetry.lease.holder : null;
  const parts = [];
  parts.push(telemetry.torque_known ? (telemetry.torque_enabled ? '토크 걸림' : '토크 없음') : '토크 모름');
  if (holder) parts.push(`${holder} 조작 중`);
  else parts.push('보기만');
  el('sub-line').textContent = parts.join(' · ');

  el('lease-idle').hidden = Boolean(lease);
  el('lease-held').hidden = !lease;
  if (telemetry.lease) el('lease-holder').textContent = telemetry.lease.holder;
  el('hold').disabled = !telemetry.running;
  document.querySelector('.tab[data-tab="status"]').classList.toggle('alert', Boolean(fault));
  paintReadings();
}

// ---------------------------------------------------------------- 조작판 짓기

/** 관절 한 줄.
 *
 * `scope`가 필요한 이유: 손목 둘과 집게는 `관절` 판과 `끝점` 판 **양쪽에** 나온다. 같은
 * `id`를 두 번 쓰면 문서에 중복 id가 생기고, `<label for>`가 늘 먼저 나온 쪽을 가리켜
 * 끝점 판에서 이름을 눌렀을 때 관절 판의 슬라이더가 잡혔다.
 */
function jointRow(item, index, scope) {
  const row = document.createElement('div');
  row.className = 'joint';
  row.dataset.joint = item.name;
  const id = `slider-${scope}-${item.name}`;
  row.innerHTML = `
    <label for="${id}">${index === null ? '' : `${index + 1}. `}${item.label}</label>
    <input id="${id}" type="range" min="${item.min}" max="${item.max}" step="0.1" value="0">
    <output>0</output>`;
  const slider = row.querySelector('input');
  slider.addEventListener('pointerdown', () => {
    commanding = canCommand();
  });
  slider.addEventListener('input', () => {
    if (!canCommand()) {
      paintControls();
      return;
    }
    commanding = true;
    setTarget(item.name, Number(slider.value));
  });
  for (const type of ['pointerup', 'pointercancel']) {
    slider.addEventListener(type, releaseGrip);
  }
  return row;
}

function buildControls() {
  const joints = el('joint-controls');
  joints.innerHTML = '';
  spec.forEach((item, index) => joints.append(jointRow(item, index, 'joint')));

  const wrists = el('wrist-controls');
  wrists.innerHTML = '';
  for (const name of HAND_JOINTS) {
    const item = specByName.get(name);
    if (item) wrists.append(jointRow(item, null, 'hand'));
  }

  const readings = el('joint-readings');
  readings.innerHTML = '';
  for (const item of spec) {
    const row = document.createElement('div');
    row.className = 'reading-row';
    row.dataset.joint = item.name;
    row.innerHTML = `<div class="name">${item.label}</div><div class="bar"><i></i></div><div class="figures"></div>`;
    readings.append(row);
  }
}

function paintControls() {
  for (const item of spec) {
    const value = target[item.name] ?? present[item.name] ?? 0;
    const measured = present[item.name];
    const reading = telemetry?.joints?.find((joint) => joint.name === item.name);
    const unit = item.unit === 'percent' ? '%' : '°';
    const digits = item.unit === 'percent' ? 0 : 1;
    for (const row of document.querySelectorAll(`.joint[data-joint="${item.name}"]`)) {
      const slider = row.querySelector('input');
      if (document.activeElement !== slider) slider.value = String(value);
      slider.disabled = !canCommand();
      // 목표와 실제가 벌어져 있으면 둘 다 적는다. 막혀서 선 팔에서는 이 차이가 곧 무슨
      // 일이 벌어졌는지에 대한 설명이고, 하나만 적으면 화면이 실제와 다른 말을 하게 된다.
      row.querySelector('output').textContent =
        measured !== undefined && Math.abs(measured - value) > 0.5
          ? `${value.toFixed(digits)}→${measured.toFixed(digits)}${unit}`
          : `${value.toFixed(digits)}${unit}`;
      row.classList.toggle('limited', Boolean(reading?.rate_limited));
    }
  }
}

function paintReadings() {
  const trip = policy.load_trip || 550;
  for (const item of spec) {
    const row = document.querySelector(`.reading-row[data-joint="${item.name}"]`);
    if (!row) continue;
    const reading = telemetry?.joints?.find((joint) => joint.name === item.name);
    if (!reading) continue;
    const load = Math.min(1, Math.abs(reading.load) / trip);
    row.querySelector('.bar i').style.width = `${(load * 100).toFixed(0)}%`;
    row.classList.toggle('hot', load > 0.6);
    row.classList.toggle('tripped', telemetry?.fault?.joint === item.name);
    const cell = row.querySelector('.figures');
    const unit = item.unit === 'percent' ? '%' : '°';
    cell.textContent =
      `${reading.present.toFixed(item.unit === 'percent' ? 0 : 1)}${unit} · ` +
      `부하 ${Math.round(Math.abs(reading.load))} · ${Math.round(reading.temperature)}°C`;
    const warm = policy.temperature_warn_c || 58;
    const hot = policy.temperature_trip_c || 65;
    cell.className =
      'figures' + (reading.temperature >= hot ? ' bad' : reading.temperature >= warm || load > 0.6 ? ' warm' : '');
  }
  el('policy-line').textContent = policy.max_deg_per_s
    ? `최대 속도 ${Math.round(policy.max_deg_per_s)}°/s · 막혔을 때 미는 거리 ${policy.lead_deg}° · ` +
      `${policy.following_error_deg}° 벌어진 채 ${policy.following_error_ms}ms 서 있으면 멈춥니다. ` +
      `속도는 서보 자신이 지키고, 판정은 전부 서버가 합니다.`
    : '';
}

// ---------------------------------------------------------------- 탭과 모드

function setTab(next) {
  tab = next;
  document.body.className = document.body.className
    .replace(/\btab-\w+\b/g, '')
    .trim() + ` tab-${next}`;
  for (const button of document.querySelectorAll('.tab')) {
    button.classList.toggle('on', button.dataset.tab === next);
  }
  const sheetOpen = next === 'lease';
  el('sheet').hidden = !sheetOpen;
  el('sheet-scrim').hidden = !sheetOpen;
  requestAnimationFrame(resize);
}

function setMode(next) {
  mode = next === 'endpoint' ? 'endpoint' : 'joint';
  localStorage.setItem('soarm-mode', mode);
  document.body.classList.toggle('mode-endpoint', mode === 'endpoint');
  document.body.classList.toggle('mode-joint', mode === 'joint');
  for (const button of document.querySelectorAll('.seg')) {
    button.classList.toggle('on', button.dataset.mode === mode);
  }
  // 끝점 모드에서는 화면이 고정된다. 끌면 시점이 따라 도는 화면에서는 끝점을 어디로
  // 보내는지 알 수 없다 — 손가락 하나가 두 가지 일을 하게 되기 때문이다.
  controls.enabled = mode === 'joint';
  if (mode === 'endpoint') applyFixedView();
  releaseGrip();
}

for (const button of document.querySelectorAll('.tab')) {
  button.addEventListener('click', () => setTab(button.dataset.tab));
}
for (const button of document.querySelectorAll('.seg')) {
  button.addEventListener('click', () => setMode(button.dataset.mode));
}
el('fold').addEventListener('click', () => {
  const folded = document.body.classList.toggle('folded');
  localStorage.setItem('soarm-folded', folded ? '1' : '0');
  requestAnimationFrame(resize);
});
if (localStorage.getItem('soarm-folded') === '1') document.body.classList.add('folded');
el('sheet-close').addEventListener('click', () => setTab('drive'));
el('sheet-scrim').addEventListener('click', () => setTab('drive'));
el('turn-left').addEventListener('click', () => {
  viewIndex = (viewIndex + 3) % 4;
  applyFixedView();
});
el('turn-right').addEventListener('click', () => {
  viewIndex = (viewIndex + 1) % 4;
  applyFixedView();
});
el('view-name').addEventListener('click', () => {
  viewIndex = 0;
  applyFixedView();
});

// ---------------------------------------------------------------- 권한

/** 서버가 요구하는 문구. 화면이 사람에게 요구하는 것과는 다르다.
 *
 * 조작 권한은 한 세션에 여러 번 받는다 — 잠깐 반납했다가 다시 잡고, 멈췄다가 다시
 * 시작한다. 그때마다 열세 글자를 치게 하면 게이트가 아니라 통행세가 되고, 통행세는
 * 사람을 신중하게 만들지 않는다. 대신 한 번의 분명한 행동을 요구한다 — 현장을 확인했다는
 * 체크. 위험의 크기는 확인의 무게가 아니라 그 화면이 무엇을 말해 주느냐로 표현한다. */
const ARM_CONFIRMATION = 'MOVE SOARM101';
const RELEASE_CONFIRMATION = 'RELEASE TORQUE SOARM101';

function refreshTakeButton() {
  el('take').disabled = !(el('confirm').checked && el('token').value.trim().length > 0);
  el('release-go').disabled = !el('release-confirm').checked;
}
el('token').addEventListener('input', refreshTakeButton);
el('confirm').addEventListener('change', refreshTakeButton);
el('release-confirm').addEventListener('change', refreshTakeButton);

const savedToken = localStorage.getItem('soarm-motion-token');
if (savedToken) el('token').value = savedToken;

/** 서버가 영어로 돌려주는 거절을, 무엇을 해야 하는지로 옮긴다.
 *
 * 맥 앱은 이미 그렇게 하고 있었는데 폰 화면은 원문을 그대로 띄웠다. 빨간 띠에
 * `Motion token is missing or wrong`이라고만 떠 있으면, 읽는 사람은 그것이 자기 토큰
 * 이야기인지 서버 설정 이야기인지 알 수 없다. **모르는 문장은 원문을 남긴다** — 옮기지
 * 못한 것을 지어내는 것보다 낫다. */
const ENGLISH = [
  [/Motion token is missing or wrong/i,
   '조작 토큰이 다릅니다. 서버의 SOARM_MOTION_TOKEN과 같은 값인지 확인하세요.'],
  [/is not configured on the server/i,
   '서버에 조작 토큰이 설정되어 있지 않습니다. 그동안은 어떤 조작 권한도 발급되지 않습니다.'],
  [/Confirmation phrase does not match/i,
   '확인 문구가 맞지 않습니다.'],
  [/Enable torque first/i,
   '먼저 토크를 걸어야 합니다. 토크가 없으면 팔은 목표를 따라갈 수 없습니다.'],
  [/already running/i, '이미 돌고 있습니다.'],
  [/Torque is still enabled/i,
   '토크가 걸려 있어 내릴 수 없습니다. 팔을 받칠 수 있을 때 토크를 먼저 푸세요.'],
  [/Device is owned by ([^\s:]+)/i, '다른 프로그램이 팔을 쥐고 있습니다: $1'],
  [/held by ([^\s]+)/i, '$1가 조작 권한을 쥐고 있습니다.'],
];

function korean(message) {
  const text = String(message ?? '');
  for (const [pattern, replacement] of ENGLISH) {
    if (pattern.test(text)) return text.replace(pattern, replacement);
  }
  return text;
}

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
    // 한 번의 확인으로 어디서든 시작할 수 있어야 한다. 관찰이 꺼져 있든, 걸려서 멈춰
    // 있든, 토크를 풀어 두었든 마찬가지다.
    if (!telemetry?.running) await post('/api/vleader/start');
    if (!telemetry?.torque_enabled) await post('/api/vleader/arm', { confirmation: ARM_CONFIRMATION });
    // 리스에도 같은 문구가 붙는다. 토크가 이미 걸려 있으면 위 줄을 지나치기 때문이다.
    lease = await post('/api/vleader/lease', {
      confirmation: ARM_CONFIRMATION,
      holder: HOLDER,
      session_id: SESSION,
    });
    el('confirm').checked = false; // 확인은 한 번 쓰고 내린다.
    target = { ...present };
    paintState();
    setTab('drive');
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
  const go = el('torque-dialog').returnValue === 'release' && el('release-confirm').checked;
  el('release-confirm').checked = false;
  refreshTakeButton();
  if (!go) return;
  try {
    await post('/api/vleader/torque/release', { confirmation: RELEASE_CONFIRMATION });
  } catch (error) {
    showReject({ message: String(error.message || error) });
  }
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

// 화면을 떠나면 조작 권한을 놓는다. 만료를 기다리는 5초는 팔이 안전해지는 시간이 아니라
// 아무도 못 만지는 시간이다.
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

// ---------------------------------------------------------------- 조작감

async function loadProfiles() {
  if (HOST === 'native') return;
  try {
    const answer = await (await fetch('/api/vleader/policy')).json();
    profiles = answer.profiles || [];
    profileName = answer.profile;
    policy = answer.policy || policy;
    paintProfiles();
  } catch (error) {
    console.warn('policy unavailable', error);
  }
}

function paintProfiles() {
  const host = el('feel-choices');
  host.innerHTML = '';
  for (const item of profiles) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = item.title;
    button.classList.toggle('on', item.name === profileName);
    button.addEventListener('click', async () => {
      try {
        const answer = await post('/api/vleader/policy', { profile: item.name });
        policy = answer.policy;
        profileName = answer.profile;
        paintProfiles();
        paintReadings();
      } catch (error) {
        showReject({ message: String(error.message || error) });
      }
    });
    host.append(button);
  }
  const chosen = profiles.find((item) => item.name === profileName);
  el('feel-detail').textContent = chosen
    ? chosen.detail
    : '지금은 세 가지 가운데 어느 것도 아닙니다 — 값을 직접 바꿔 두었습니다.';
}

// ---------------------------------------------------------------- 카메라

function openCamera() {
  if (HOST === 'native') return;
  const image = el('camera-image');
  image.src = `/api/cameras/${selectedCamera}.mjpg?v=${Date.now()}`;
  for (const chip of document.querySelectorAll('.chip')) {
    chip.classList.toggle('on', chip.dataset.camera === selectedCamera);
  }
}

function startCameras() {
  if (HOST === 'native') return;
  const image = el('camera-image');
  image.addEventListener('load', () => {
    image.classList.remove('down');
    el('camera-down').hidden = true;
  });
  image.addEventListener('error', () => {
    // 깨진 이미지 아이콘을 남겨 두지 않는다. 카메라가 없는 것과 화면이 고장 난 것은
    // 다른 일인데, 그 아이콘은 둘을 같아 보이게 한다.
    image.classList.add('down');
    el('camera-down').hidden = false;
    setTimeout(openCamera, 3000);
  });
  for (const chip of document.querySelectorAll('.chip')) {
    chip.addEventListener('click', () => {
      selectedCamera = chip.dataset.camera;
      localStorage.setItem('soarm-camera', selectedCamera);
      openCamera();
    });
  }
  openCamera();
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
  /** 앱이 조작 방식을 바꿨다. */
  setMode(next) {
    setMode(next);
  },
  /** 앱이 시점을 90° 돌렸다. `+1`이면 오른쪽, `-1`이면 왼쪽, `0`이면 앞으로 되돌린다. */
  turnView(direction) {
    viewIndex = direction === 0 ? 0 : (viewIndex + (direction > 0 ? 1 : 3)) % 4;
    applyFixedView();
    return VIEWS[viewIndex].name;
  },
  /** 앱의 앞뒤 손잡이. */
  setDepth(value) {
    depthHeld = Number(value) || 0;
    if (depthHeld) commanding = true;
  },
  /** 지금 페이지가 무엇을 믿고 있는가.
   *
   * 조작 경로를 밖에서 확인하기 위한 창이다. 값을 바꾸지 않고 읽기만 한다 — 시험이
   * 화면과 다른 길로 팔을 움직이면, 그 시험이 통과해도 화면은 여전히 안 될 수 있다.
   * 그래서 시험은 손가락으로 끌고, 여기서는 그 결과만 들여다본다. */
  debugState() {
    const tcp = ghost ? forward(target) : null;
    return {
      mode,
      state: telemetry?.state ?? null,
      commanding,
      target: { ...target },
      present: { ...present },
      tcp: tcp ? [tcp.x, tcp.y, tcp.z] : null,
      unreachable: !el('reach-note').hidden,
      view: VIEWS[viewIndex].name,
      canCommand: canCommand(),
      dragging,
      grabbing,
      lease: lease?.lease_id ?? null,
    };
  },

  /** 한 장 그린다. 창이 화면에 없거나 탭이 뒤에 있으면 브라우저가
   *  `requestAnimationFrame`을 멈추는데, 그때도 마지막 상태를 한 장 그려야 한다. */
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
// 만료된다. 뒤로 간 탭이 권한을 놓는 것은 맞지만, 그 결정은 `visibilitychange`가
// 명시적으로 해야 한다 — 프레임 스케줄러의 부작용으로 일어날 일이 아니다.
setInterval(beat, 800);

function tick(now) {
  requestAnimationFrame(tick);
  stepFromKeys();
  stepDepth();
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
    measureModel(live.stage);
    setMode(mode);
    if (mode === 'joint') applyFreeView();
    if (HOST === 'web') {
      const payload = await (await fetch('/api/vleader')).json();
      applyHello(payload);
      openSocket();
      startCameras();
      loadProfiles();
      refreshTakeButton();
      // 토큰이 이미 기억되어 있으면 곧바로 조작할 수 있는 화면을 연다. 홈 화면에서
      // 눌러 들어온 사람이 가장 먼저 할 일은 권한을 받는 것이고, 그때 필요한 것은
      // 체크 하나뿐이다.
      if (!savedToken) setTab('lease');
    } else {
      const context = renderer.getContext();
      const info = context && context.getExtension('WEBGL_debug_renderer_info');
      toNative({
        type: 'ready',
        renderer: info ? context.getParameter(info.UNMASKED_RENDERER_WEBGL) : 'WebGL',
        triangles: renderer.info.render.triangles,
      });
    }
    requestAnimationFrame(tick);
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

if ('serviceWorker' in navigator && HOST === 'web') {
  // 캐시를 두지 않는 워커다. 홈 화면 앱으로 설치되려면 크롬이 워커를 요구하는데,
  // 이 화면은 늘 서버와 이어져 있어야 하므로 무엇도 캐시하지 않는다 — 오래된 조작
  // 화면이 캐시에서 살아 돌아오는 것은 그 자체로 사고다.
  navigator.serviceWorker.register('/viewer/sw.js').catch(() => {});
}

boot();
