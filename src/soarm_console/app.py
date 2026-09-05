from __future__ import annotations

import re
import time
from contextlib import asynccontextmanager, suppress
from importlib.metadata import version
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .cameras import RECORDING_PROFILE, CameraProfile, CameraWorker
from .config import Settings
from .datasets import (
    NAME_PATTERN,
    DatasetError,
    TrajectoryTooLargeError,
    dataset_tasks,
    delete_episode,
    describe,
    list_datasets,
    move_to_trash,
    playable_clip,
    trajectory,
)
from .diagnostics import doctor_failure, run_hardware_doctor
from .spark import SparkBusy, SparkError, SparkNotFound
from .spark import list_datasets as spark_list_datasets
from .spark import list_runs as spark_list_runs
from .spark import probe as spark_probe
from .spark import pull_checkpoint as spark_pull_checkpoint
from .spark import push_dataset as spark_push_dataset
from .spark import start_training as spark_start_training
from .spark import stop_training as spark_stop_training
from .spark import train_command as spark_train_command
from .record_manager import RecordManager
from .replay_manager import ReplayManager
from .replaying import (
    DEFAULT_SPEED,
    SPEEDS,
    ReplayError,
    alignment_refusal,
    alignment_seconds,
    episode_first_pose,
    present_position,
    unit_of,
)
from .teleop import TeleopError, TeleopManager
from .torque import TorqueError
from .torque import release as release_torque_on
from .vleader.api import (
    RELEASE_CONFIRMATION,
    VirtualLeader,
    _authorise_motion,
    _token_from,
    build_router,
)
from .vleader.backend import HardwareError


settings = Settings()
teleop = TeleopManager(settings)
recorder = RecordManager(settings)
replayer = ReplayManager(settings)
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
    # 재생은 팔을 움직이는 중일 수 있다. 서버가 내려가면서 그것을 두고 가면 아무도
    # 멈출 수 없는 팔이 남는다. 멈추기만 하고 토크는 그대로 둔다.
    with suppress(TeleopError):
        replayer.stop()
    with suppress(TeleopError):
        recorder.stop()
    with suppress(TeleopError):
        teleop.stop()


app = FastAPI(title="SO-ARM101 Console", version="0.1.0", lifespan=lifespan)


#: 폰·태블릿에서 온 요청인가.
#:
#: 정교한 기기 판별이 아니라 "이 화면을 여기서 쓸 수 있는가"만 묻는 것이다. 콘솔은 3열
#: 데스크톱 레이아웃이고 3D 조작이 아예 없으므로, 손가락으로 온 사람에게는 어느 쪽이든
#: 조작 화면이 맞다.
_TOUCH_AGENT = re.compile(r"iPhone|iPad|iPod|Android|Mobile|Silk|Kindle", re.IGNORECASE)


@app.middleware("http")
async def no_stale_screens(request: Request, call_next):
    """조작 화면은 캐시에서 살아 돌아오지 않는다.

    옛 화면이 새 서버의 거절 코드나 새 상태 필드를 모르면, 팔이 왜 안 움직이는지 아무
    말도 하지 못한다. 실제로 폰에서 `/`를 열었는데 리다이렉트가 걸리지 않는 일이 있었고,
    서버는 새 파일을 내주고 있었으므로 남은 설명은 사파리가 들고 있던 옛 사본이었다.

    화면을 이루는 것만 막는다 — HTML·스크립트·스타일·매니페스트. 카메라 스트림과 STL
    메시는 바뀌지 않고 무거우므로 그대로 둔다. 스크립트가 빠지면 반쪽짜리가 된다: 옛
    `viewer.js`가 새 서버에 붙으면 화면은 멀쩡해 보이는데 동작만 옛것이다.
    """
    response = await call_next(request)
    kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
    stale_is_dangerous = (
        kind in {"text/html", "text/css", "application/manifest+json"}
        or "javascript" in kind
    )
    if stale_is_dangerous:
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


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
    #: 이어 찍을 데이터셋. `resume`이 참일 때만 본다. 비어 있으면 자식이 새 이름을 짓는다.
    dataset: str | None = None
    #: 참이면 새 데이터셋을 만들지 않고 `dataset`에 회차를 이어 붙인다.
    resume: bool = False


class TrainRequest(BaseModel):
    dataset: str
    policy: str = "act"


#: 이 서버가 답할 수 있는 것들. 맥 앱은 이 목록에 이름이 있을 때만 그 기능을 켠다 —
#: 화면이 서버보다 앞서 나가면 사람은 눌리지 않는 단추를 보게 되고, 서버가 앞서 나가면
#: 새 기능이 아무에게도 보이지 않는다. 이름은 글자까지 계약이다.
CAPABILITIES = [
    # 찍던 회를 버리고 끝낸다.
    "abort",
    # 기존 데이터셋에 회차를 이어 붙인다.
    "resume",
    # 수집 중인 카메라 그림을 JPEG 한 장으로 내준다.
    "preview",
    # 데이터셋 목록에 `loop_hz`와 카메라 stale 비율이 함께 실린다.
    "quality",
    # 데이터셋과 회차를 `data/.trash`로 보낸다.
    "delete",
    # 콘솔이 학습 서버에 학습을 띄우고 진행을 읽는다.
    "train",
    # 재생을 시작하기 전에 관절별 거리와 정렬 시간을 미리 본다.
    "replay_preview",
    # 팔로워가 리더 자세까지 걸어간 뒤에 루프가 시작된다.
    "soft_start",
]


class RecordControlRequest(BaseModel):
    key: str


#: 팔이 움직이는 경로는 전부 같은 게이트를 지난다. 텔레옵의 `START SOARM101`,
#: 수집의 `RECORD SOARM101`과 나란한 문구다.
REPLAY_CONFIRMATION = "REPLAY SOARM101"


class ReplayRequest(BaseModel):
    confirmation: str
    dataset: str
    episode: int = 0
    #: 0.25 / 0.5 / 1.0. 기본은 절반이다 — 처음 보는 재생은 느린 편이 낫다.
    speed: float = DEFAULT_SPEED


class CameraSettingsRequest(BaseModel):
    width: int
    height: int
    fps: int


def camera_status(worker: CameraWorker, role: str | None = None) -> dict[str, object]:
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
        "recording_controls": recorder.camera_controls(role) if role is not None else None,
    }


def device_status(path: str) -> dict[str, object]:
    device = Path(path)
    return {
        "path": path,
        "exists": device.exists(),
        "resolved": str(device.resolve()) if device.exists() else None,
    }


# `response_model=None`: 돌려주는 것이 파일일 수도 리다이렉트일 수도 있는데, FastAPI는
# 그 합집합으로 응답 모델을 만들려다 실패한다(`Invalid args for response field`). 여기서
# 만들 스키마가 없다는 것을 분명히 적어 둔다.
@app.get("/", response_model=None)
def index(request: Request) -> FileResponse | RedirectResponse:
    """콘솔 첫 화면. **손가락으로 온 사람은 조작 화면으로 보낸다.**

    이 페이지는 3열 데스크톱 콘솔이고 3D 조작이 아예 없다. 그런데 서버 주소를 폰에서
    그냥 열면 여기로 온다 — 사용자가 실제로 여기 도착해서 "3D 조작이 구현이 안 되어
    있는데?"라고 물었다. 이 화면이 조작 화면의 존재를 한 글자도 말하지 않았으니 맞는
    말이었다.

    판정을 **서버에서** 한다. 화면 안의 자바스크립트로도 같은 일을 하지만 그쪽은 옛
    사본이 캐시에 남아 있으면 실행되지 않는다. 실제로 고친 뒤에도 폰에서 조작 화면이
    뜨지 않는 일이 있었고, 서버는 새 파일을 내주고 있었다. 헤더는 캐시를 타지 않는다.

    `?console=1`이면 폰에서도 이 화면을 그대로 본다. 되돌아올 길이 없으면 전체 콘솔은
    주소를 아는 사람만 볼 수 있게 된다.
    """
    if "console" not in request.query_params and _TOUCH_AGENT.search(
        request.headers.get("user-agent", "")
    ):
        return RedirectResponse("/viewer/?host=web", status_code=307)
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
        "cameras": {name: camera_status(worker, name) for name, worker in cameras.items()},
        # 수집은 프리뷰에서 무엇을 고르든 이 값으로 돌아간다. 화면이 그렇게 말할 수 있도록
        # 값을 숨기지 않고 내보인다.
        "recording_profile": RECORDING_PROFILE.as_dict(),
        "capabilities": CAPABILITIES,
        "preflight": teleop.preflight(),
        "teleop_preflight": teleop.preflight(),
        "record_preflight": recorder.preflight(),
        "teleoperation": teleop.status(),
        "recording": recorder.status(),
        "replay": replayer.status(),
        "replay_preflight": replayer.preflight(),
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


@app.get("/api/datasets/{name}/episodes/{episode_index}/trajectory")
def dataset_trajectory(name: str, episode_index: int) -> dict[str, object]:
    try:
        return trajectory(name, episode_index)
    except TrajectoryTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"No such dataset episode: {name}/{episode_index}",
        ) from exc
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


@app.get("/api/spark")
def spark_status() -> dict[str, object]:
    """Reachability, GPU and free disk on the training machine."""
    return spark_probe(settings)


@app.get("/api/spark/datasets")
def spark_datasets() -> list[dict[str, object]]:
    """Datasets already synced to the training machine."""
    try:
        return spark_list_datasets(settings)
    except SparkError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/spark/datasets/{name}")
def spark_push(name: str) -> dict[str, object]:
    """Copy one recorded dataset to the training machine.

    This blocks for as long as the copy takes. That is honest for a dataset of a
    few episodes over tailnet, and wrong for a large one — if recordings grow past
    a few minutes of transfer, this should become a background job with a status
    endpoint, the way recording already is."""
    try:
        return spark_push_dataset(settings, name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No such dataset: {name}") from exc
    except DatasetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SparkError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/spark/runs")
def spark_runs() -> list[dict[str, object]]:
    """Training runs on the training machine, with their checkpoints."""
    try:
        return spark_list_runs(settings)
    except SparkError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/spark/train")
def spark_train_start(request: TrainRequest) -> dict[str, object]:
    """학습 서버에서 학습을 띄운다. tmux 안에서 돌고, 콘솔은 그 뒤로 진행만 읽는다.

    콘솔이 학습 프로세스를 품지 않는 것이 요점이다. 학습은 몇 시간 돌고 콘솔은
    `systemctl --user restart`로 다시 시작되는 서비스라, 둘의 수명이 묶이면 콘솔을
    고치는 일이 곧 학습을 죽이는 일이 된다.
    """
    try:
        return spark_start_training(settings, request.dataset, request.policy)
    except DatasetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SparkNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SparkBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SparkError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# 이 경로는 아래 `/{step}`보다 **먼저** 적혀야 한다. FastAPI는 적힌 순서대로 맞춰 보므로,
# 뒤에 두면 `stop`이 체크포인트 이름으로 읽힌다.
@app.post("/api/spark/runs/{run}/stop")
def spark_train_stop(run: str) -> dict[str, object]:
    """도는 학습을 멈춘다. 이미 쓰인 체크포인트는 그대로 남는다."""
    try:
        return spark_stop_training(settings, run)
    except DatasetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SparkNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SparkError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/spark/runs/{run}/{step}")
def spark_pull(run: str, step: str) -> dict[str, object]:
    """Fetch one checkpoint back for local inference."""
    try:
        return spark_pull_checkpoint(settings, run, step)
    except DatasetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SparkError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/spark/train-command")
def spark_train(
    name: str, policy: str = "act", steps: int = 100_000, batch_size: int = 64
) -> dict[str, object]:
    """The command to start training, for a person to run in a terminal.

    The console does not start training itself: it runs for hours, which does not
    fit the lifetime of a web request, and a console restart would take the run
    with it."""
    try:
        command = spark_train_command(
            settings, name, policy=policy, steps=steps, batch_size=batch_size
        )
    except DatasetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"command": command}


@app.post("/api/doctor")
def doctor() -> dict[str, object]:
    global last_doctor
    if teleop.running or recorder.running or vleader.running:
        raise HTTPException(status_code=409, detail="Cannot inspect serial buses during an active mode")
    last_doctor = run_hardware_doctor(settings)
    return last_doctor


class TorqueReleaseRequest(BaseModel):
    arm: str
    confirmation: str


@app.post("/api/torque/release")
def release_torque(request: Request, body: TorqueReleaseRequest) -> dict[str, object]:
    """Let one arm go limp, on purpose.

    A previous session leaves torque on — nothing here turns it off on exit, because a
    fault that drops the arm is worse than a fault that holds it. The cost is that the
    next teleop is refused until someone releases it, and until now the console had no
    way to do that outside the virtual leader. This is that way.

    It writes to the motors, so it needs the motion token and the same typed phrase the
    virtual leader asks for. It refuses while a mode is running: that process owns the
    bus, and the arm is holding a pose it was told to hold."""
    _authorise_motion(_token_from(request))
    if body.confirmation != RELEASE_CONFIRMATION:
        raise HTTPException(status_code=400, detail="Confirmation phrase does not match")
    if teleop.running or recorder.running or vleader.running:
        raise HTTPException(
            status_code=409,
            detail="Stop the running mode before releasing torque",
        )
    try:
        return release_torque_on(settings, body.arm)
    except TorqueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/teleoperation/start")
def start_teleoperation(request: MotionRequest) -> dict[str, object]:
    global last_doctor
    if request.confirmation != "START SOARM101":
        raise HTTPException(status_code=400, detail="Confirmation phrase does not match")
    if recorder.running:
        raise HTTPException(status_code=409, detail="Stop recording before teleoperation")
    if replayer.running:
        raise HTTPException(
            status_code=409,
            detail="Stop the replay before teleoperation: the follower has one owner",
        )
    if vleader.running:
        raise HTTPException(
            status_code=409,
            detail="Stop the virtual leader before physical-leader teleoperation: the follower has one owner",
        )
    last_doctor = run_hardware_doctor(settings)
    if not last_doctor["healthy"]:
        # 토크가 걸려 있는지는 더 이상 묻지 않는다 — `diagnostics.run_hardware_doctor`가
        # 그 이유를 적어 두었다. 여기서 막는 것은 모터가 답하지 않거나 전압이 이상한
        # 경우뿐이고, 어느 팔의 무엇인지를 문구에 담는다.
        raise HTTPException(
            status_code=409,
            detail=f"Hardware doctor did not pass: {doctor_failure(last_doctor)}",
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


def _check_resumable(name: str | None, task: str) -> None:
    """이 데이터셋에 이어 찍어도 되는가.

    과제가 같은지를 보는 이유는, 데이터셋 하나가 학습 한 번의 단위이기 때문이다. LeRobot
    자체는 과제가 다른 회차도 같은 폴더에 넣어 주지만, 그렇게 섞인 데이터는 파케이를 열기
    전에는 섞였다는 사실조차 보이지 않는다. 여기서 막는 편이 낫다.

    로봇·fps·feature가 맞는지는 여기서 보지 않는다. `record()`가 `LeRobotDataset.resume`
    뒤에 `sanity_check_dataset_robot_compatibility`로 확인하고, 그 검사가 훨씬 정확하다.
    """
    if not name or not NAME_PATTERN.match(name):
        raise HTTPException(status_code=404, detail=f"No such dataset: {name}")
    try:
        # `dataset_tasks`가 `meta/info.json`이 있는지까지 본다 — 없으면 `FileNotFoundError`다.
        tasks = dataset_tasks(name)
    except (FileNotFoundError, DatasetError) as exc:
        raise HTTPException(status_code=404, detail=f"No such dataset: {name}") from exc
    if tasks != [task.strip()]:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Dataset task does not match: dataset has {tasks}, "
                f"request has {[task.strip()]}"
            ),
        )


@app.post("/api/recording/start")
def start_recording(request: RecordRequest) -> dict[str, object]:
    global last_doctor
    if request.confirmation != "RECORD SOARM101":
        raise HTTPException(status_code=400, detail="Confirmation phrase does not match")
    if teleop.running:
        raise HTTPException(status_code=409, detail="Stop teleoperation before recording")
    if replayer.running:
        raise HTTPException(
            status_code=409,
            detail="Stop the replay before recording: the follower has one owner",
        )
    if request.teleop not in {"leader", "virtual"}:
        raise HTTPException(status_code=400, detail="teleop must be 'leader' or 'virtual'")
    if request.resume:
        _check_resumable(request.dataset, request.task)
    if request.teleop == "leader":
        if vleader.running:
            raise HTTPException(
                status_code=409, detail="Stop the virtual leader before recording with the physical leader"
            )
        last_doctor = run_hardware_doctor(settings)
        if not last_doctor["healthy"]:
            raise HTTPException(
                status_code=409,
                detail=f"Hardware doctor did not pass: {doctor_failure(last_doctor)}",
            )
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
            request.task,
            request.episodes,
            request.episode_seconds,
            teleop_source=request.teleop,
            dataset=request.dataset,
            resume=request.resume,
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


#: 이보다 오래된 스냅숏은 내주지 않는다. 5Hz로 갱신되므로 3초는 열다섯 번을 놓쳤다는
#: 뜻이고, 그때 화면에 뜨는 그림은 "지금 무엇을 찍고 있나"에 대한 답이 아니다.
PREVIEW_MAX_AGE_S = 3.0


@app.get("/api/recording/preview/{role}.jpg")
def recording_preview(role: str) -> FileResponse:
    """수집 중인 카메라가 방금 본 것. 한 장짜리 JPEG.

    수집이 도는 동안 콘솔의 MJPEG 스트림은 꺼져 있다 — 카메라를 쥔 것은 record 자식이고
    장치 하나를 두 프로세스가 열 수는 없다. 그래서 찍는 쪽이 파일로 내려놓은 그림을
    여기서 그대로 내준다.

    오래된 그림은 404다. 멈춘 카메라의 마지막 장면을 계속 내주면, 화면은 아무 일도
    없다는 듯 그것을 보여 준다.
    """
    if role not in cameras:
        raise HTTPException(status_code=404, detail="Unknown camera")
    if not recorder.running:
        raise HTTPException(status_code=404, detail="Recording is not running")
    path = recorder.preview_path(role)
    try:
        age = time.time() - path.stat().st_mtime
    except OSError as exc:
        raise HTTPException(status_code=404, detail="No preview yet") from exc
    if age > PREVIEW_MAX_AGE_S:
        raise HTTPException(status_code=404, detail="No preview yet")
    return FileResponse(
        path,
        media_type="image/jpeg",
        # 이 파일은 자리를 지키고 내용만 바뀐다. 캐시에 한 장이 남으면 화면은 그 한 장을
        # 계속 보여 주면서 갱신되고 있다고 믿는다.
        headers={"Cache-Control": "no-store"},
    )


@app.delete("/api/datasets/{name}")
def delete_dataset(name: str) -> dict[str, object]:
    """데이터셋 하나를 `data/.trash`로 보낸다. **지우지는 않는다.**

    몇 시간짜리 시연을 담은 폴더를 웹 요청 하나가 영구히 없애도 되는 이유가 없다.
    목록에서 사라지는 것으로 충분하고, 디스크를 실제로 비우는 것은 사람이
    `rm -rf data/.trash`로 한다.
    """
    if recorder.running or replayer.running:
        raise HTTPException(
            status_code=409, detail="Cannot delete while recording or replaying"
        )
    try:
        moved = move_to_trash(name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No such dataset: {name}") from exc
    except DatasetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"name": name, "trashed_to": str(moved)}


@app.delete("/api/datasets/{name}/episodes/{episode_index}")
def delete_dataset_episode(name: str, episode_index: int) -> dict[str, object]:
    """회차 하나를 데이터셋에서 들어내고, 남은 것을 돌려준다.

    찍다 만 회차가 데이터셋 안에서 온전한 시연인 척하는 것을 고치는 길이다. 앞으로
    찍는 것은 `abort`가 막지만, 이미 들어 있는 것을 꺼낼 자리도 있어야 한다.
    """
    if recorder.running or replayer.running:
        raise HTTPException(
            status_code=409, detail="Cannot delete while recording or replaying"
        )
    try:
        return delete_episode(name, episode_index)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"No such dataset episode: {name}/{episode_index}"
        ) from exc
    except DatasetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/replay/preview")
def replay_preview(dataset: str, episode: int = 0) -> dict[str, object]:
    """시작하기 전에, 팔이 어디서 어디로 갈지.

    거리 제한을 사실상 없앴으므로(`replaying.ALIGN_REFUSE_DISTANCE`) 판단은 사람이 한다.
    판단에는 재료가 필요하다 — 관절마다 지금 어디에 있고 어디로 가야 하는지, 그리고 그
    걸음에 몇 초가 걸리는지. 맥 앱의 확인 시트가 이 값을 그대로 적는다.

    팔로워 serial을 읽으므로 다른 모드가 돌고 있으면 거절한다. 장치 하나에 소유자는
    하나다.
    """
    if recorder.running or teleop.running or vleader.running or replayer.running:
        raise HTTPException(
            status_code=409,
            detail="Stop the running mode before reading the follower: it has one owner",
        )
    try:
        goal = episode_first_pose(dataset, episode)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"No such dataset or episode: {dataset} #{episode}"
        ) from exc
    except DatasetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        start = present_position(settings)
    except ReplayError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # 순서는 데이터셋이 정한 feature 순서 그대로다. 화면의 표가 매번 같은 줄에 같은
    # 관절을 놓으려면 그 순서가 흔들리면 안 된다.
    joints = [
        {
            "name": name,
            "from": start.get(name),
            "to": value,
            "distance": None if name not in start else abs(value - start[name]),
            "unit": unit_of(name),
        }
        for name, value in goal.items()
    ]
    refusal = alignment_refusal(start, goal)
    try:
        seconds = alignment_seconds(start, goal)
    except ReplayError:
        # 거리를 잴 수 없는 자세다. 그 이유는 `refusal`이 이미 말하고 있다.
        seconds = None
    return {
        "dataset": dataset,
        "episode": episode,
        "joints": joints,
        "align_seconds": seconds,
        "refusal": refusal,
    }


@app.post("/api/replay/start")
def start_replay(request: ReplayRequest) -> dict[str, object]:
    """찍은 에피소드를 실제 팔에 다시 흘린다.

    팔이 사람 손 없이 혼자 움직이는 유일한 경로다. 그래서 게이트가 셋이다: 텔레옵·수집과
    같은 확인 문구, 모션 게이트, 그리고 **팔이 지금 그 에피소드가 시작하는 자리 근처에
    있는가**. 마지막 것이 이 경로에만 있는 이유는, 녹화의 첫 자세가 팔이 지금 서 있는
    자세와 아무 관계가 없기 때문이다.
    """
    if request.confirmation != REPLAY_CONFIRMATION:
        raise HTTPException(status_code=400, detail="Confirmation phrase does not match")
    if replayer.running:
        raise HTTPException(status_code=409, detail="Stop the replay that is already running")
    if recorder.running:
        raise HTTPException(
            status_code=409,
            detail="Stop recording before replaying: the follower has one owner",
        )
    if teleop.running:
        raise HTTPException(
            status_code=409,
            detail="Stop teleoperation before replaying: the follower has one owner",
        )
    if vleader.running:
        raise HTTPException(
            status_code=409,
            detail="Stop the virtual leader before replaying: the follower has one owner",
        )
    if not settings.motion_enabled:
        raise HTTPException(
            status_code=400, detail="SOARM_ENABLE_MOTION=1 is required before the arm may move"
        )
    if request.speed not in SPEEDS:
        raise HTTPException(
            status_code=400, detail=f"speed must be one of {[float(value) for value in SPEEDS]}"
        )
    try:
        goal = episode_first_pose(request.dataset, request.episode)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"No such dataset or episode: {request.dataset} #{request.episode}",
        ) from exc
    except DatasetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        start = present_position(settings)
    except ReplayError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    refusal = alignment_refusal(start, goal)
    if refusal:
        raise HTTPException(status_code=400, detail=refusal)
    try:
        replayer.start(request.dataset, request.episode, request.speed)
    except TeleopError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return replayer.status()


@app.post("/api/replay/stop")
def stop_replay() -> dict[str, object]:
    """멈춘다. 토크는 걸어 둔 채 팔이 그 자리에 선다 — 멈추는 것과 힘을 놓는 것은 다르다."""
    replayer.request_stop()
    return replayer.status()


@app.post("/api/mode/stop")
def stop_active_mode() -> dict[str, object]:
    try:
        # 재생을 먼저 세운다. 이 셋 가운데 사람 손 없이 팔이 혼자 움직이는 것은
        # 재생뿐이므로, 급한 손이 누르는 단추는 그것부터 멈춰야 한다.
        if replayer.running:
            replayer.stop()
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
        "replay": replayer.status(),
        "virtual_leader": vleader.status(),
    }
