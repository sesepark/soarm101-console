const $ = (selector) => document.querySelector(selector);
let state = null;
let pendingAction = null;

const readyRow = (label, ok, detail = '') => `<div class="ready-row"><span>${label}</span><span class="${ok ? 'ok' : 'no'}">${ok ? 'READY' : 'REQUIRED'}${detail ? ` · ${detail}` : ''}</span></div>`;
function scrollLogToBottom() {
  const log = $('#logs');
  requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
}

async function request(path, method = 'GET', body) {
  const options = {method, headers: {'Content-Type': 'application/json'}, cache: 'no-store'};
  if (body) options.body = JSON.stringify(body);
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

function switchView(name) {
  document.querySelectorAll('.view-tab').forEach((tab) => {
    const active = tab.dataset.view === name;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-selected', String(active));
  });
  document.querySelectorAll('.view-panel').forEach((panel) => {
    const active = panel.dataset.panel === name;
    panel.classList.toggle('active', active);
    panel.hidden = !active;
  });
  history.replaceState(null, '', `#${name}`);
}

async function refresh() {
  try {
    state = await request('/api/status');
    const teleop = state.teleoperation.running;
    const recording = state.recording.running;
    const running = teleop || recording;
    const teleopReady = state.teleop_preflight.length === 0;
    const recordReady = state.record_preflight.length === 0;
    const doctor = state.doctor;
    const mode = recording ? 'RECORD' : teleop ? 'TELEOP' : 'IDLE';

    $('#overall').textContent = running ? `● ${mode}` : teleopReady ? '● READY' : '● SETUP REQUIRED';
    $('#overall').className = `status-pill ${running ? 'running' : teleopReady ? 'ready' : 'blocked'}`;
    $('#version').textContent = `LeRobot ${state.software.lerobot}`;
    $('#readiness').innerHTML = [
      readyRow('Leader bus', doctor ? doctor.arms.leader.healthy : state.devices.leader.exists, doctor ? `${doctor.arms.leader.voltage_raw.shoulder_pan / 10}V` : ''),
      readyRow('Follower bus', doctor ? doctor.arms.follower.healthy : state.devices.follower.exists, doctor ? `${doctor.arms.follower.voltage_raw.shoulder_pan / 10}V` : ''),
      readyRow('Leader calibration', state.calibrations.leader.exists),
      readyRow('Follower calibration', state.calibrations.follower.exists),
    ].join('');
    $('#mode').textContent = mode;
    $('#teleop-mode-mirror').textContent = mode;
    $('#pid').textContent = running ? `pid ${recording ? state.recording.pid : state.teleoperation.pid}` : 'pid —';
    $('#motion-note').textContent = state.motion_enabled ? 'Motion gate가 열려 있습니다. 현장 확인 후에만 시작하세요.' : '현재 observation-only입니다. 캘리브레이션 후 motion gate를 열 수 있습니다.';
    $('#start-teleop').disabled = !teleopReady || running;
    $('#start-record').disabled = !recordReady || running;
    $('#stop-all').disabled = !running;

    const runtime = state.recording.runtime || {};
    $('#record-phase').textContent = recording ? (runtime.phase || 'STARTING').toUpperCase() : 'INACTIVE';
    $('#record-dataset').textContent = runtime.dataset_name ? `dataset · ${runtime.dataset_name}` : 'dataset —';
    for (const id of ['record-success', 'record-retry', 'record-stop', 'record-abort']) $(`#${id}`).disabled = !recording;
    const logs = recording ? state.recording.logs : state.teleoperation.logs;
    if (logs.length) {
      $('#logs').textContent = logs.join('\n');
      scrollLogToBottom();
    }
    for (const name of ['scene', 'wrist']) $(`#${name}-state`).textContent = state.cameras[name].active ? '수신 중' : '대기';
  } catch (error) {
    $('#overall').textContent = '● API OFFLINE';
    $('#overall').className = 'status-pill blocked';
  }
}

function cameraStart(name) {
  const image = $(`#${name}-image`);
  image.src = `/api/cameras/${name}.mjpg?v=${Date.now()}`;
  image.classList.add('active');
}

async function cameraStop(name) {
  await request(`/api/cameras/${name}/stop`, 'POST');
  const image = $(`#${name}-image`);
  image.removeAttribute('src');
  image.classList.remove('active');
  await refresh();
}

function showConfirm(action) {
  pendingAction = action;
  const recording = action === 'record';
  $('#dialog-title').textContent = recording ? '데이터 수집 시작' : '텔레오퍼레이션 시작';
  $('#dialog-copy').textContent = recording ? '카메라 프리뷰를 종료하고 새 로컬 dataset 세션을 시작합니다.' : '두 팔을 대응되는 비슷한 자세로 맞춘 뒤 시작하세요. 리더 관절값을 팔로워에 전달합니다.';
  $('#confirmation').value = '';
  $('#confirmation').placeholder = recording ? 'RECORD SOARM101' : 'START SOARM101';
  $('#safety-check').checked = false;
  $('#dialog-confirm').disabled = true;
  $('#action-result').textContent = '';
  $('#motion-dialog').showModal();
}

document.querySelectorAll('.view-tab').forEach((tab) => tab.addEventListener('click', () => switchView(tab.dataset.view)));
$('#dialog-confirm').addEventListener('click', async (event) => {
  event.preventDefault();
  try {
    if (pendingAction === 'record') {
      await request('/api/recording/start', 'POST', {confirmation: $('#confirmation').value, task: $('#record-task').value, episodes: Number($('#record-episodes').value), episode_seconds: Number($('#record-seconds').value)});
    } else {
      await request('/api/teleoperation/start', 'POST', {confirmation: $('#confirmation').value});
    }
    $('#motion-dialog').close();
  } catch (error) {
    $('#action-result').textContent = error.message;
  }
  await refresh();
});
$('#safety-check').addEventListener('change', (event) => { $('#dialog-confirm').disabled = !event.target.checked; });
$('#start-teleop').addEventListener('click', () => showConfirm('teleop'));
$('#start-record').addEventListener('click', () => showConfirm('record'));
$('#doctor').addEventListener('click', async () => {
  switchView('observe');
  try {
    $('#logs').textContent = '읽기 전용 하드웨어 진단 중…';
    $('#logs').textContent = JSON.stringify(await request('/api/doctor', 'POST'), null, 2);
    scrollLogToBottom();
  } catch (error) {
    $('#logs').textContent = error.message;
    scrollLogToBottom();
  }
  await refresh();
});
$('#stop-all').addEventListener('click', async () => { try { await request('/api/mode/stop', 'POST'); } catch (error) { alert(error.message); } await refresh(); });
document.querySelectorAll('[data-camera-start]').forEach((button) => button.addEventListener('click', () => cameraStart(button.dataset.cameraStart)));
document.querySelectorAll('[data-camera-stop]').forEach((button) => button.addEventListener('click', () => cameraStop(button.dataset.cameraStop)));
document.querySelectorAll('[data-camera-focus]').forEach((button) => button.addEventListener('click', () => {
  const name = button.dataset.cameraFocus;
  cameraStart(name);
  $('#focus-title').textContent = name === 'scene' ? 'Scene camera' : 'Wrist camera';
  $('#focus-image').src = $(`#${name}-image`).src;
  $('#focus-view').classList.add('open');
  $('#focus-view').setAttribute('aria-hidden', 'false');
}));
$('#focus-close').addEventListener('click', () => { $('#focus-view').classList.remove('open'); $('#focus-view').setAttribute('aria-hidden', 'true'); $('#focus-image').removeAttribute('src'); });
$('#record-success').addEventListener('click', async () => { await request('/api/recording/control', 'POST', {key: 'right'}); await refresh(); });
$('#record-retry').addEventListener('click', async () => { await request('/api/recording/control', 'POST', {key: 'left'}); await refresh(); });
$('#record-stop').addEventListener('click', async () => { await request('/api/recording/control', 'POST', {key: 'esc'}); await refresh(); });
// `esc`는 루프를 빠져나온 뒤 save_episode()가 그대로 돌아 찍다 만 회를 저장한다. 그 회가
// 필요 없을 때 누르는 것이 이쪽이다 — 버퍼를 비우고 나간다.
$('#record-abort').addEventListener('click', async () => { await request('/api/recording/control', 'POST', {key: 'abort'}); await refresh(); });
$('#clear-log').addEventListener('click', () => { $('#logs').textContent = '로그를 지웠습니다.'; scrollLogToBottom(); });

switchView(['observe', 'teleop', 'dataset'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'observe');
refresh();
setInterval(refresh, 2000);
