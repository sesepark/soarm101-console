from __future__ import annotations

from contextlib import asynccontextmanager, suppress
from importlib.metadata import version
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .cameras import RECORDING_PROFILE, CameraProfile, CameraWorker
from .config import Settings
from .datasets import DatasetError, describe, list_datasets, playable_clip
from .diagnostics import run_hardware_doctor
from .record_manager import RecordManager
from .teleop import TeleopError, TeleopManager


settings = Settings()
teleop = TeleopManager(settings)
recorder = RecordManager(settings)
cameras = {
    "scene": CameraWorker(settings.scene_camera),
    "wrist": CameraWorker(settings.wrist_camera),
}
last_doctor: dict[str, object] | None = None
static_dir = Path(__file__).with_name("static")


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    for worker in cameras.values():
        worker.stop()
    with suppress(TeleopError):
        recorder.stop()
    with suppress(TeleopError):
        teleop.stop()


app = FastAPI(title="SO-ARM101 Console", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


class MotionRequest(BaseModel):
    confirmation: str


class RecordRequest(BaseModel):
    confirmation: str
    task: str
    episodes: int = 10
    episode_seconds: int = 30


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
def mobile() -> FileResponse:
    """Phone-first, observation-only camera viewer."""
    return FileResponse(static_dir / "mobile.html")


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
    if teleop.running or recorder.running:
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
    last_doctor = run_hardware_doctor(settings)
    if not last_doctor["safe_for_motion_start"]:
        raise HTTPException(status_code=409, detail="Hardware doctor did not pass")
    for worker in cameras.values():
        worker.stop()
    try:
        recorder.start(request.task, request.episodes, request.episode_seconds)
    except TeleopError as exc:
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
    return {"teleoperation": teleop.status(), "recording": recorder.status()}
