from __future__ import annotations

from contextlib import asynccontextmanager, suppress
from importlib.metadata import version
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .cameras import RECORDING_PROFILE, CameraProfile, CameraWorker
from .config import Settings
from .datasets import DatasetError, describe, list_datasets, playable_clip
from .diagnostics import run_hardware_doctor
from .record_manager import RecordManager
from .teleop import TeleopError, TeleopManager
from .vleader.api import VirtualLeader, build_router
from .vleader.backend import HardwareError


settings = Settings()
teleop = TeleopManager(settings)
recorder = RecordManager(settings)
cameras = {
    "scene": CameraWorker(settings.scene_camera),
    "wrist": CameraWorker(settings.wrist_camera),
}
vleader = VirtualLeader(settings)
last_doctor: dict[str, object] | None = None
static_dir = Path(__file__).with_name("static")


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    for worker in cameras.values():
        worker.stop()
    # 가상 리더가 팔로워 serial을 쥐고 있으면 여기서 놓는다. `force=True`인 이유는
    # 프로세스가 내려가는 자리라 사람에게 물어볼 수 없기 때문이다 — 토크는 그대로 두고
    # 루프만 세운다. 팔은 마지막 자세를 유지한 채 남는다.
    with suppress(Exception):
        vleader.stop(force=True)
    with suppress(TeleopError):
        recorder.stop()
    with suppress(TeleopError):
        teleop.stop()


app = FastAPI(title="SO-ARM101 Console", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=static_dir), name="static")
# 3D 뷰어는 한 번만 만들고 서버가 서빙한다. 맥 앱이 `WKWebView`로 품는 화면과 폰의
# 조작 화면이 같은 파일을 쓴다 — 구현이 둘이면 두 기기의 동작이 반드시 어긋난다.
app.mount("/viewer", StaticFiles(directory=static_dir / "viewer", html=True), name="viewer")
app.include_router(build_router(vleader))


class MotionRequest(BaseModel):
    confirmation: str


class RecordRequest(BaseModel):
    confirmation: str
    task: str
    episodes: int = 10
    episode_seconds: int = 30
    #: `leader`는 물리 리더 팔, `virtual`은 3D 뷰어로 만드는 가상 리더.
    teleop: str = "leader"


class RecordControlRequest(BaseModel):
    key: str


class CameraSettingsRequest(BaseModel):
    width: int
    height: int
    fps: int


def camera_status(worker: CameraWorker) -> dict[str, object]:
    """카메라 한 대의 지금 상태.

    `requested`와 `actual`을 따로 내보내는 이유가 있다. 드라이버는 못 맞추는 해상도를
    거절하는 대신 가까운 값으로 바꿔 열어 버리기 때문에, 고른 값만 보여 주면 화면이
    실제와 다른 말을 하게 된다.
    """
    actual = worker.actual
    return {
        "active": worker.active,
        "clients": worker.clients,
        "error": worker.error,
        "requested": worker.profile.as_dict(),
        "actual": actual.as_dict() if actual else None,
        "modes": worker.modes(),
    }


def device_status(path: str) -> dict[str, object]:
    device = Path(path)
    return {
        "path": path,
        "exists": device.exists(),
        "resolved": str(device.resolve()) if device.exists() else None,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/mobile", include_in_schema=False)
@app.get("/mobile/", include_in_schema=False)
def mobile() -> RedirectResponse:
    """폰에서 여는 자리. 이제 조작 화면 하나로 모인다.

    예전에는 여기가 카메라만 보여 주는 별도의 화면이었다. 화면이 둘이면 폰에서 영상을
    보다가 조작하려고 다른 주소로 옮겨 가야 했고, 그 사이 화면 생김새도 달라졌다.
    지금은 `/viewer/`의 `카메라` 칸이 같은 일을 하므로 이 주소는 그리로 보낸다 —
    폰에 남아 있는 옛 북마크와 홈 화면 아이콘이 빈 자리로 가지 않게 하는 길이다.
    """
    return RedirectResponse("/viewer/?host=web", status_code=307)


@app.get("/viewer/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    """홈 화면 앱의 이름과 아이콘.

    `StaticFiles`에 맡기지 않고 여기서 내보내는 이유는 미디어 타입 하나 때문이다.
    파이썬의 `mimetypes`는 `.webmanifest`를 모르고, 그러면 `text/plain`으로 나간다.
    크롬은 그 응답을 매니페스트로 읽지 않으므로 "홈 화면에 추가"가 제안되지 않는다.
    """
    return FileResponse(
        static_dir / "viewer/manifest.webmanifest", media_type="application/manifest+json"
    )


@app.get("/viewer/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    """서비스 워커. 캐시는 두지 않는다 — 오래된 조작 화면이 살아 돌아오는 것은 사고다.

    범위(scope)를 `/`로 두려면 워커가 `/`에서 나가야 하는데, 여기서는 `/viewer/`면
    충분하다. 그래서 `Service-Worker-Allowed`도 붙이지 않는다.
    """
    return FileResponse(static_dir / "viewer/sw.js", media_type="text/javascript")


@app.get("/api/status")
def status() -> dict[str, object]:
    return {
        "motion_enabled": settings.motion_enabled,
        "camera_roles_confirmed": settings.camera_roles_confirmed,
        "max_relative_target": settings.max_relative_target,
        "devices": {
            "leader": device_status(settings.leader_port),
            "follower": device_status(settings.follower_port),
            "scene_camera": device_status(settings.scene_camera),
            "wrist_camera": device_status(settings.wrist_camera),
        },
        "calibrations": {
            "leader": {
                "path": str(settings.leader_calibration),
                "exists": settings.leader_calibration.exists(),
            },
            "follower": {
                "path": str(settings.follower_calibration),
                "exists": settings.follower_calibration.exists(),
            },
        },
        "software": {"lerobot": version("lerobot")},
        "cameras": {name: camera_status(worker) for name, worker in cameras.items()},
        # 수집은 프리뷰에서 무엇을 고르든 이 값으로 돌아간다. 화면이 그렇게 말할 수 있도록
        # 값을 숨기지 않고 내보인다.
        "recording_profile": RECORDING_PROFILE.as_dict(),
        "preflight": teleop.preflight(),
        "teleop_preflight": teleop.preflight(),
        "record_preflight": recorder.preflight(),
        "teleoperation": teleop.status(),
        "recording": recorder.status(),
        "virtual_leader": vleader.status(),
        "doctor": last_doctor,
    }


@app.get("/api/cameras/{name}.mjpg")
def camera_stream(name: str) -> StreamingResponse:
    worker = cameras.get(name)
    if worker is None:
        raise HTTPException(status_code=404, detail="Unknown camera")
    if not Path(worker.path).exists():
        raise HTTPException(status_code=503, detail=f"Camera is not connected: {worker.path}")
    return StreamingResponse(
        worker.frames(), media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.post("/api/cameras/{name}/settings")
def configure_camera(name: str, request: CameraSettingsRequest) -> dict[str, object]:
    """프리뷰 화질과 프레임을 바꾼다. 수집 중에는 받지 않는다.

    수집이 도는 동안 카메라를 쥐고 있는 것은 LeRobot 쪽이고, 그때의 설정은 고정이다.
    여기서 바꿀 수 있게 두면 화면에는 새 값이 뜨는데 기록되는 데이터는 그대로인,
    정확히 헷갈리기 좋은 상태가 된다.
    """
    worker = cameras.get(name)
    if worker is None:
        raise HTTPException(status_code=404, detail="Unknown camera")
    if recorder.running:
        raise HTTPException(
            status_code=409,
            detail=(
                "Recording fixes every camera at "
                f"{RECORDING_PROFILE.width}x{RECORDING_PROFILE.height}@{RECORDING_PROFILE.fps}"
            ),
        )
    profile = CameraProfile(width=request.width, height=request.height, fps=request.fps)
    if profile.fps < 1 or profile.width < 1 or profile.height < 1:
        raise HTTPException(status_code=400, detail="Camera settings must be positive")
    if not worker.supports(profile):
        raise HTTPException(
            status_code=400,
            detail=f"Camera cannot do {profile.width}x{profile.height}@{profile.fps}",
        )
    worker.configure(profile)
    return {"ok": True, "camera": name, **camera_status(worker)}


@app.post("/api/cameras/{name}/stop")
def stop_camera(name: str) -> dict[str, object]:
    worker = cameras.get(name)
    if worker is None:
        raise HTTPException(status_code=404, detail="Unknown camera")
    worker.stop()
    return {"ok": True, "camera": name}


@app.get("/api/datasets")
def datasets() -> list[dict[str, object]]:
    """Recorded datasets, newest metadata first read from disk.

    Read-only, and deliberately served from here rather than read over SSH by a
    client: the on-disk layout is LeRobot's, the episode metadata is parquet, and
    the only process that should need to know either is the one that wrote it.
    """
    return list_datasets()


@app.get("/api/datasets/{name}")
def dataset(name: str) -> dict[str, object]:
    try:
        return describe(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No such dataset: {name}") from exc
    except DatasetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/datasets/{name}/video/{video_key}/{chunk_index}/{file_index}")
def dataset_video(
    name: str, video_key: str, chunk_index: int, file_index: int,
    from_: float = Query(0.0, alias="from"), to: float = Query(0.0),
) -> FileResponse:
    """One episode, as something the caller can actually play.

    Several episodes share a video file, so the range is part of the request. The
    recording itself is AV1, which Apple Silicon cannot decode, so this hands back
    an H.264 cut of just those seconds — the dataset on disk stays exactly what
    LeRobot wrote."""
    try:
        path = playable_clip(name, video_key, chunk_index, file_index, from_, to)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="No such video file") from exc
    except DatasetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return FileResponse(path, media_type="video/mp4")


@app.post("/api/doctor")
def doctor() -> dict[str, object]:
    global last_doctor
    if teleop.running or recorder.running or vleader.running:
        raise HTTPException(status_code=409, detail="Cannot inspect serial buses during an active mode")
    last_doctor = run_hardware_doctor(settings)
    return last_doctor


@app.post("/api/teleoperation/start")
def start_teleoperation(request: MotionRequest) -> dict[str, object]:
    global last_doctor
    if request.confirmation != "START SOARM101":
        raise HTTPException(status_code=400, detail="Confirmation phrase does not match")
    if recorder.running:
        raise HTTPException(status_code=409, detail="Stop recording before teleoperation")
    if vleader.running:
        raise HTTPException(
            status_code=409,
            detail="Stop the virtual leader before physical-leader teleoperation: the follower has one owner",
        )
    last_doctor = run_hardware_doctor(settings)
    if not last_doctor["safe_for_motion_start"]:
        raise HTTPException(
            status_code=409,
            detail="Read-only hardware doctor did not confirm healthy buses with torque disabled",
        )
    try:
        teleop.start()
    except TeleopError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return teleop.status()


@app.post("/api/teleoperation/stop")
def stop_teleoperation() -> dict[str, object]:
    try:
        teleop.stop()
    except TeleopError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return teleop.status()


@app.post("/api/recording/start")
def start_recording(request: RecordRequest) -> dict[str, object]:
    global last_doctor
    if request.confirmation != "RECORD SOARM101":
        raise HTTPException(status_code=400, detail="Confirmation phrase does not match")
    if teleop.running:
        raise HTTPException(status_code=409, detail="Stop teleoperation before recording")
    if request.teleop not in {"leader", "virtual"}:
        raise HTTPException(status_code=400, detail="teleop must be 'leader' or 'virtual'")
    if request.teleop == "leader":
        if vleader.running:
            raise HTTPException(
                status_code=409, detail="Stop the virtual leader before recording with the physical leader"
            )
        last_doctor = run_hardware_doctor(settings)
        if not last_doctor["safe_for_motion_start"]:
            raise HTTPException(status_code=409, detail="Hardware doctor did not pass")
    else:
        # 가상 리더로 찍는다. 팔로워 serial의 소유자는 이제 record 프로세스이므로
        # 콘솔은 장치를 놓고 목표만 중계한다. 진단은 돌리지 않는다 — 그 진단도 serial을
        # 여는 일이고, 가상 리더가 이미 열어 두고 읽고 있었다.
        try:
            vleader.start_relay()
        except HardwareError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    for worker in cameras.values():
        worker.stop()
    try:
        recorder.start(
            request.task, request.episodes, request.episode_seconds, teleop_source=request.teleop
        )
    except TeleopError as exc:
        if request.teleop == "virtual":
            with suppress(Exception):
                vleader.stop(force=True)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return recorder.status()


@app.post("/api/recording/control")
def control_recording(request: RecordControlRequest) -> dict[str, object]:
    try:
        recorder.control(request.key)
    except TeleopError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return recorder.status()


@app.post("/api/mode/stop")
def stop_active_mode() -> dict[str, object]:
    try:
        if recorder.running:
            recorder.stop()
        if teleop.running:
            teleop.stop()
    except TeleopError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    # 가상 리더는 여기서 **멈추기만** 한다. 내리지 않는 이유는 토크가 걸려 있을 수 있고,
    # 그때 루프를 세우면 팔을 붙잡아 줄 것이 사라지기 때문이다. 자세를 유지한 채 선다.
    if vleader.owner is not None:
        vleader.owner.hold()
    return {
        "teleoperation": teleop.status(),
        "recording": recorder.status(),
        "virtual_leader": vleader.status(),
    }
