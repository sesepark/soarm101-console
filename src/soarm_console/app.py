from __future__ import annotations

from contextlib import asynccontextmanager, suppress
from importlib.metadata import version
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .cameras import CameraWorker
from .config import Settings
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
        "cameras": {
            name: {"active": worker.active, "clients": worker.clients, "error": worker.error}
            for name, worker in cameras.items()
        },
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


@app.post("/api/cameras/{name}/stop")
def stop_camera(name: str) -> dict[str, object]:
    worker = cameras.get(name)
    if worker is None:
        raise HTTPException(status_code=404, detail="Unknown camera")
    worker.stop()
    return {"ok": True, "camera": name}


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
