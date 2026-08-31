const image = document.querySelector('#camera-image');
const placeholder = document.querySelector('#camera-placeholder');
const placeholderTitle = document.querySelector('#placeholder-title');
const placeholderCopy = document.querySelector('#placeholder-copy');
const retryButton = document.querySelector('#retry');
const connection = document.querySelector('#connection');
const cameraName = document.querySelector('#camera-name');
const cameraNumber = document.querySelector('#camera-number');
const cameraDetail = document.querySelector('#camera-detail');

let selectedCamera = 'scene';
let status = null;
let streamOpen = false;
let retryTimer = null;
let retryCount = 0;

const labels = {
  scene: {number: 'CAM 01', name: 'SCENE'},
  wrist: {number: 'CAM 02', name: 'WRIST'},
};

function setConnection(kind, message) {
  connection.className = `connection ${kind}`;
  connection.querySelector('span').textContent = message;
}

function showPlaceholder(title, copy, failed = false) {
  image.classList.remove('visible');
  placeholder.hidden = false;
  placeholder.classList.toggle('failed', failed);
  placeholderTitle.textContent = title;
  placeholderCopy.textContent = copy;
  retryButton.hidden = !failed;
}

function closeStream() {
  streamOpen = false;
  image.classList.remove('visible');
  image.removeAttribute('src');
}

function cameraIsAvailable() {
  return Boolean(status?.devices?.[`${selectedCamera}_camera`]?.exists);
}

function recordingOwnsCameras() {
  return Boolean(status?.recording?.running);
}

function openStream() {
  clearTimeout(retryTimer);
  if (recordingOwnsCameras()) {
    closeStream();
    showPlaceholder('카메라 사용 중', '데이터 수집이 끝나면 자동으로 다시 연결합니다.');
    setConnection('waiting', '수집 중');
    return;
  }
  if (!cameraIsAvailable()) {
    closeStream();
    showPlaceholder('카메라 없음', `${labels[selectedCamera].name} 카메라가 서버에 연결되지 않았습니다.`, true);
    setConnection('error', '카메라 없음');
    return;
  }
  streamOpen = true;
  showPlaceholder('카메라 연결 중', '첫 프레임을 기다리고 있습니다.');
  setConnection('waiting', '영상 연결 중');
  image.src = `/api/cameras/${selectedCamera}.mjpg?v=${Date.now()}`;
}

function scheduleReconnect() {
  closeStream();
  if (recordingOwnsCameras()) return;
  retryCount += 1;
  const delay = Math.min(1000 * (2 ** Math.min(retryCount - 1, 3)), 8000);
  showPlaceholder('영상 연결 끊김', `${Math.round(delay / 1000)}초 후 다시 연결합니다.`, true);
  setConnection('error', '연결 끊김');
  clearTimeout(retryTimer);
  retryTimer = setTimeout(openStream, delay);
}

function updateCameraDetail() {
  const camera = status?.cameras?.[selectedCamera];
  const profile = camera?.actual || camera?.requested;
  if (!profile) {
    cameraDetail.textContent = '상태 확인 중';
    return;
  }
  const fps = camera.actual ? `${profile.fps} FPS` : '연결 대기';
  cameraDetail.textContent = `${profile.width}×${profile.height} · ${fps}`;
}

async function refreshStatus() {
  try {
    const response = await fetch('/api/status', {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const wasRecording = recordingOwnsCameras();
    status = await response.json();
    updateCameraDetail();

    if (recordingOwnsCameras()) {
      if (streamOpen || !wasRecording) openStream();
    } else if (wasRecording || !streamOpen) {
      openStream();
    }
  } catch (error) {
    setConnection('error', '서버 연결 끊김');
    if (!streamOpen) showPlaceholder('서버 연결 실패', 'Tailscale 연결과 서버 상태를 확인하세요.', true);
  }
}

function selectCamera(name) {
  if (name === selectedCamera && streamOpen) return;
  selectedCamera = name;
  retryCount = 0;
  clearTimeout(retryTimer);
  closeStream();
  cameraName.textContent = labels[name].name;
  cameraNumber.textContent = labels[name].number;
  document.querySelectorAll('.camera-choice').forEach((button) => {
    const active = button.dataset.camera === name;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  updateCameraDetail();
  openStream();
}

image.addEventListener('load', () => {
  retryCount = 0;
  placeholder.hidden = true;
  image.classList.add('visible');
  setConnection('live', '실시간 영상');
});
image.addEventListener('error', scheduleReconnect);
retryButton.addEventListener('click', () => {
  retryCount = 0;
  openStream();
});
document.querySelectorAll('.camera-choice').forEach((button) => {
  button.addEventListener('click', () => selectCamera(button.dataset.camera));
});
window.addEventListener('pagehide', () => {
  clearTimeout(retryTimer);
  closeStream();
});

refreshStatus();
setInterval(refreshStatus, 3000);
