// URDF 한 대를 three.js 트리로 옮기는 최소 구현.
//
// `urdf-loader` 패키지를 쓰지 않는 이유는 의존성 하나를 줄이려는 것이 아니라, 이 뷰어가
// 인터넷 없는 집 서버에서 서빙되고 맥의 WKWebView와 아이폰 사파리가 그대로 읽어야 하기
// 때문이다. 필요한 것은 link/joint/visual 세 가지뿐이고, 그 세 가지는 여기 100줄이면 된다.
//
// 좌표계: URDF는 Z-up, three.js는 Y-up이다. 루트를 X축으로 -90° 돌려 세운다.
import * as THREE from 'three';
import { STLLoader } from 'three/STLLoader.js';

const loader = new STLLoader();

function parseVector(text, fallback) {
  if (!text) return fallback;
  const parts = text.trim().split(/\s+/).map(Number);
  return parts.length === 3 && parts.every(Number.isFinite) ? parts : fallback;
}

function applyOrigin(object, element) {
  const origin = element ? element.querySelector(':scope > origin') : null;
  const xyz = parseVector(origin && origin.getAttribute('xyz'), [0, 0, 0]);
  const rpy = parseVector(origin && origin.getAttribute('rpy'), [0, 0, 0]);
  object.position.set(xyz[0], xyz[1], xyz[2]);
  // URDF의 rpy는 고정축 XYZ 순서다. three.js에서 같은 회전을 만드는 순서가 'ZYX'다.
  object.rotation.set(rpy[0], rpy[1], rpy[2], 'ZYX');
}

/**
 * @param {string} url       .urdf 주소
 * @param {string} meshBase  mesh filename을 이어 붙일 기준 주소
 */
export async function loadURDF(url, meshBase) {
  const text = await (await fetch(url, { cache: 'force-cache' })).text();
  const xml = new DOMParser().parseFromString(text, 'application/xml');
  const robot = xml.querySelector('robot');
  if (!robot) throw new Error('URDF에 robot 요소가 없습니다');

  const materials = new Map();
  for (const node of robot.querySelectorAll(':scope > material')) {
    const color = node.querySelector('color');
    const rgba = parseVector(color && color.getAttribute('rgba'), [0.8, 0.8, 0.8]);
    materials.set(node.getAttribute('name'), rgba);
  }

  const links = new Map();
  const pending = [];
  for (const node of robot.querySelectorAll(':scope > link')) {
    const group = new THREE.Group();
    group.name = node.getAttribute('name');
    links.set(group.name, group);
    for (const visual of node.querySelectorAll(':scope > visual')) {
      const mesh = visual.querySelector('geometry > mesh');
      if (!mesh) continue;
      const holder = new THREE.Group();
      applyOrigin(holder, visual);
      group.add(holder);
      const named = visual.querySelector(':scope > material');
      const rgba = materials.get(named && named.getAttribute('name')) || [0.75, 0.76, 0.8, 1];
      const file = mesh.getAttribute('filename').replace(/^package:\/\/[^/]+\//, '');
      pending.push(
        loader.loadAsync(`${meshBase}${file}`).then((geometry) => {
          geometry.computeVertexNormals();
          const material = new THREE.MeshStandardMaterial({
            color: new THREE.Color(rgba[0], rgba[1], rgba[2]),
            metalness: 0.15,
            roughness: 0.62,
          });
          const scale = parseVector(mesh.getAttribute('scale'), [1, 1, 1]);
          const object = new THREE.Mesh(geometry, material);
          object.scale.set(scale[0], scale[1], scale[2]);
          // 어느 관절을 잡았는지 되짚기 위한 표. 링크 이름만 있으면 충분하다.
          object.userData.link = group.name;
          holder.add(object);
        })
      );
    }
  }

  const joints = new Map();
  const children = new Set();
  for (const node of robot.querySelectorAll(':scope > joint')) {
    const name = node.getAttribute('name');
    const type = node.getAttribute('type');
    const parent = links.get(node.querySelector('parent').getAttribute('link'));
    const child = links.get(node.querySelector('child').getAttribute('link'));
    if (!parent || !child) continue;
    const pivot = new THREE.Group();
    pivot.name = `joint:${name}`;
    applyOrigin(pivot, node);
    parent.add(pivot);
    pivot.add(child);
    children.add(child.name);
    if (type === 'revolute' || type === 'continuous' || type === 'prismatic') {
      const axisNode = node.querySelector(':scope > axis');
      const axis = new THREE.Vector3(
        ...parseVector(axisNode && axisNode.getAttribute('xyz'), [0, 0, 1])
      ).normalize();
      const limit = node.querySelector(':scope > limit');
      joints.set(name, {
        name,
        type,
        pivot,
        axis,
        child,
        rest: pivot.quaternion.clone(),
        lower: limit ? Number(limit.getAttribute('lower')) : -Math.PI,
        upper: limit ? Number(limit.getAttribute('upper')) : Math.PI,
        value: 0,
      });
    }
  }

  await Promise.all(pending);

  const roots = [...links.values()].filter((link) => !children.has(link.name));
  const robotGroup = new THREE.Group();
  robotGroup.name = robot.getAttribute('name') || 'robot';
  roots.forEach((root) => robotGroup.add(root));
  // Z-up을 세운다.
  const stage = new THREE.Group();
  stage.rotation.x = -Math.PI / 2;
  stage.add(robotGroup);

  // 어느 링크가 어느 관절에 매달려 있는지.
  //
  // 링크를 집었을 때 돌려야 하는 것은 **그 링크를 매달고 있는 가장 안쪽 관절**이다.
  // 그래서 관절마다 자기 subtree를 칠하는 대신 루트에서 한 번 내려가며 "지금까지 지나온
  // 마지막 관절"을 들고 다닌다. 관절 목록의 순서에 답이 달라지지 않는다.
  const linkToJoint = new Map();
  const paint = (group, jointName) => {
    if (jointName) linkToJoint.set(group.name, jointName);
    for (const item of group.children) {
      if (item.name.startsWith('joint:')) {
        paint(item.children[0], item.name.slice('joint:'.length));
      }
    }
  };
  roots.forEach((root) => paint(root, null));

  function setJoint(name, radians) {
    const joint = joints.get(name);
    if (!joint) return;
    joint.value = radians;
    if (joint.type === 'prismatic') {
      joint.child.position.copy(joint.axis).multiplyScalar(radians);
      return;
    }
    const rotation = new THREE.Quaternion().setFromAxisAngle(joint.axis, radians);
    joint.pivot.quaternion.copy(joint.rest).multiply(rotation);
  }

  return { stage, robot: robotGroup, joints, linkToJoint, setJoint };
}
