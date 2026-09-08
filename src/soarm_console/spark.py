from __future__ import annotations

import json
import subprocess
import time
from pathlib import PurePosixPath
from typing import Any

from .config import Settings
from .datasets import NAME_PATTERN, DatasetError, data_root
from .models import dimension_problems, model_dir


class SparkError(RuntimeError):
    pass


class SparkNotFound(SparkError):
    """찾는 것이 학습 기계에 없다. `app`이 404로 옮긴다."""


class SparkBusy(SparkError):
    """학습 기계가 이미 그 일을 하고 있다. `app`이 409로 옮긴다."""


# 원격 명령은 항상 인자 리스트로 만들고 셸을 거치지 않는다. 데이터셋 이름이 그대로 원격
# 경로가 되므로, 셸을 한 번이라도 거치면 이름 하나로 임의 명령을 실행할 수 있다.
# 이름 규칙은 `datasets.py`의 것을 그대로 재사용한다 — 검사 지점을 둘로 나누면 한쪽만
# 고쳐지는 날이 온다.
SSH_OPTIONS = [
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "ConnectTimeout=10",
]

# 원격에서 JSON을 만들어 오는 스크립트는 `python3 -`의 표준입력으로 넘긴다. 인자로 넘기면
# 따옴표를 이스케이프해야 하고, 그 이스케이프가 곧 주입 경로가 된다.
_LIST_DATASETS = """
import json, os, sys
root = sys.argv[1]
out = []
if os.path.isdir(root):
    for name in sorted(os.listdir(root)):
        info = os.path.join(root, name, "meta", "info.json")
        if not os.path.isfile(info):
            continue
        try:
            with open(info, encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            continue
        size = 0
        for base, _dirs, files in os.walk(os.path.join(root, name)):
            for filename in files:
                try:
                    size += os.path.getsize(os.path.join(base, filename))
                except OSError:
                    pass
        out.append({
            "name": name,
            "episodes": meta.get("total_episodes", 0),
            "frames": meta.get("total_frames", 0),
            "fps": meta.get("fps", 0),
            "robot_type": meta.get("robot_type"),
            "codebase_version": meta.get("codebase_version"),
            "size_bytes": size,
            "synced_at": os.path.getmtime(info),
        })
print(json.dumps(out))
"""

# 학습을 띄우기 전에 원격에서 한 번에 확인하는 것들: 데이터셋이 와 있는가, 이미 도는
# 학습이 있는가. 둘을 따로 물으면 그 사이에 다른 사람이 학습을 시작할 틈이 생긴다.
_TRAIN_PREFLIGHT = """
import json, os, subprocess, sys
dataset_root, = sys.argv[1:2]
present = os.path.isfile(os.path.join(os.path.expanduser(dataset_root), "meta", "info.json"))
running = []
try:
    out = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"], capture_output=True, text=True
    )
    if out.returncode == 0:
        running = [
            name.strip()[len("train-"):]
            for name in out.stdout.splitlines()
            if name.strip().startswith("train-")
        ]
except OSError:
    pass
print(json.dumps({"dataset_present": present, "running": running}))
"""

# 적을 내용은 인자가 아니라 **스크립트 안에** 넣는다. `_remote_python`의 argv는 ssh가
# 공백으로 이어 붙여 원격 셸이 다시 가르는 자리라, 따옴표와 공백이 든 JSON은 그 길로
# 넘어가지 못한다. `json.dumps`가 만든 문자열 리터럴은 파이썬 소스로서 안전하다.
_WRITE_TRAIN_META = """
import json, os, sys
directory = os.path.expanduser(sys.argv[1])
os.makedirs(directory, exist_ok=True)
with open(os.path.join(directory, "soarm_train.json"), "w", encoding="utf-8") as handle:
    handle.write(__PAYLOAD__)
print(json.dumps({"ok": True}))
"""

_REMOTE_MODEL = """
import json, os, sys
root = os.path.expanduser(sys.argv[1])
def read(name):
    with open(os.path.join(root, name), encoding="utf-8") as handle:
        return json.load(handle)
config = read("config.json")
train = read("train_config.json")
processor = read("policy_preprocessor.json")
rename_map = {}
for step in processor.get("steps", []):
    if step.get("registry_name") == "rename_observations_processor":
        rename_map = (step.get("config") or {}).get("rename_map") or {}
        break
def dim(features, key):
    shape = (features.get(key) or {}).get("shape") or []
    return shape[0] if shape else None
inputs = config.get("input_features") or {}
images = [name for name, value in inputs.items()
          if (value or {}).get("type") == "VISUAL" or name.startswith("observation.images.")]
dataset = train.get("dataset") or {}
print(json.dumps({
    "policy": config.get("type"), "dataset": dataset.get("repo_id"),
    "trained_steps": train.get("steps"), "chunk_size": config.get("chunk_size"),
    "n_action_steps": config.get("n_action_steps"), "image_features": images,
    "state_dim": dim(inputs, "observation.state"),
    "action_dim": dim(config.get("output_features") or {}, "action"),
    "rename_map": rename_map,
}))
"""

_PROBE = """
import json, shutil, subprocess
info = {"reachable": True}
try:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,temperature.gpu,power.draw",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=15,
    )
    if out.returncode == 0 and out.stdout.strip():
        name, used, total, temp, power = [f.strip() for f in out.stdout.strip().splitlines()[0].split(",")]

        # GB10은 통합메모리라 nvidia-smi가 GPU 전용 메모리를 모른다고 답한다. 값이 없는
        # 것과 읽기에 실패한 것은 다르므로, 없는 값은 None으로 두고 예외로 만들지 않는다.
        def number(text, cast):
            try:
                return cast(float(text))
            except ValueError:
                return None

        info["gpu"] = {
            "name": name,
            "memory_used_mib": number(used, int),
            "memory_total_mib": number(total, int),
            "temperature_c": number(temp, int),
            "power_w": number(power, float),
        }
except Exception as error:
    info["gpu_error"] = str(error)
usage = shutil.disk_usage("/")
info["disk_free_bytes"] = usage.free
info["disk_total_bytes"] = usage.total
print(json.dumps(info))
"""


def _target(settings: Settings) -> str:
    return f"{settings.spark_user}@{settings.spark_host}"


# 실패 이유를 사람이 읽을 수 있는 한 줄로.
#
# rsync와 ssh는 원인을 첫 줄에 찍고 마지막 줄에는 요약을 남긴다. 마지막 줄만 집으면
# `rsync error: unexplained error (code 255)` 같은, 아무것도 설명하지 않는 문장을
# 그대로 사람에게 보여 주게 된다. 아는 원인이 있으면 그것을 말하고, 없으면 적어도
# 원인이 담긴 줄을 고른다. 문장은 영어로 둔다 — 서버가 내는 `detail`을 화면의 말로
# 옮기는 자리는 클라이언트에 이미 있고(`SOArmServerText`), 그 규칙을 여기서 깨지 않는다.
_CAUSES: list[tuple[str, str]] = [
    ("No space left on device", "The training machine has no free disk space"),
    ("Disk quota exceeded", "The training machine has no free disk space"),
    ("Could not resolve hostname", "Cannot find the training machine on the network"),
    ("Name or service not known", "Cannot find the training machine on the network"),
    ("Permission denied", "The training machine refused the login"),
    ("Host key verification failed", "The training machine's host key changed"),
    ("Connection refused", "The training machine refused the connection"),
    ("Connection timed out", "The training machine did not answer"),
    ("No route to host", "The training machine did not answer"),
    ("Broken pipe", "The connection dropped during transfer"),
    ("Connection closed", "The connection dropped during transfer"),
    ("connection unexpectedly closed", "The connection dropped during transfer"),
]

# rsync 종료 코드. 원인 문장을 못 찾았을 때 마지막으로 기대는 자리다.
_RSYNC_CODES: dict[int, str] = {
    11: "The training machine could not write the files",
    12: "The transfer protocol failed",
    23: "Some files were not transferred",
    24: "Some files vanished during transfer",
    30: "The transfer timed out",
    255: "The connection dropped during transfer",
}


def _explain(args: list[str], returncode: int, output: str) -> str:
    lines = [line.strip() for line in output.strip().splitlines() if line.strip()]
    for needle, message in _CAUSES:
        if any(needle.lower() in line.lower() for line in lines):
            return message
    if args and args[0] == "rsync" and returncode in _RSYNC_CODES:
        return _RSYNC_CODES[returncode]
    # 아는 것이 없으면 원문을 준다. 다만 "unexplained error" 같은 요약 줄보다는 그 앞의
    # 줄이 대체로 원인에 가깝다.
    for line in lines:
        if "unexplained error" not in line and "error in rsync protocol" not in line:
            return line
    return lines[-1] if lines else f"exit {returncode}"


def _run(args: list[str], *, timeout: float, stdin: str | None = None) -> str:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=stdin,
        )
    except subprocess.TimeoutExpired as error:
        raise SparkError(f"The transfer did not finish within {timeout:.0f} seconds") from error
    except OSError as error:
        raise SparkError(str(error)) from error
    if result.returncode != 0:
        raise SparkError(_explain(args, result.returncode, (result.stderr or "") + "\n" + (result.stdout or "")))
    return result.stdout


def _remote_python(settings: Settings, script: str, *argv: str, timeout: float = 60) -> Any:
    args = ["ssh", *SSH_OPTIONS, _target(settings), "python3", "-", *argv]
    raw = _run(args, timeout=timeout, stdin=script)
    try:
        return json.loads(raw)
    except ValueError as error:
        raise SparkError("The training machine sent something that is not JSON") from error


def _queue_request(
    settings: Settings, method: str, path: str, payload: dict[str, Any] | None = None
) -> Any:
    """Call sparkq through SSH without exposing its loopback-only HTTP port."""
    if method not in {"GET", "POST", "DELETE"} or not path.startswith("/api/"):
        raise ValueError("Invalid sparkq request")
    body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
    # Spark's login policy allows the queue's loopback curl API and rejects ad-hoc remote Python.
    # Feed JSON over stdin: putting it in SSH argv would make the remote shell parse its quotes again.
    raw = _run(
        [
            "ssh",
            *SSH_OPTIONS,
            _target(settings),
            "curl",
            "-sS",
            "-X",
            method,
            "-HContent-Type:application/json",
            "--data-binary",
            "@-",
            "-w",
            "%{http_code}",
            f"http://127.0.0.1:{settings.spark_queue_port}{path}",
        ],
        timeout=30,
        stdin=body,
    )
    if len(raw) < 3 or not raw[-3:].isdigit():
        raise SparkError("sparkq did not return an HTTP status")
    status = int(raw[-3:])
    response = raw[:-3]
    if not 200 <= status < 300:
        detail = response or "sparkq request failed"
        try:
            decoded = json.loads(detail)
            detail = str(decoded.get("detail") or detail)
        except (TypeError, ValueError):
            pass
        if status == 409:
            raise SparkBusy(detail)
        raise SparkError(detail)
    if not response:
        return {}
    try:
        return json.loads(response)
    except ValueError as exc:
        raise SparkError("sparkq returned a response that is not JSON") from exc


def policy_checkpoint_path(settings: Settings, run: str, step: str) -> str:
    """The pretrained path is interpreted by the policy server on Spark."""
    if not NAME_PATTERN.fullmatch(run) or not NAME_PATTERN.fullmatch(step):
        raise DatasetError("Unknown run or step")
    root = PurePosixPath(settings.spark_output_root)
    if not root.is_absolute():
        root = PurePosixPath(settings.spark_home) / root
    return str(root / run / "checkpoints" / step / "pretrained_model")


def describe_remote_model(settings: Settings, run: str, step: str) -> dict[str, Any]:
    """Read only the small checkpoint metadata on Spark; weights remain there."""
    path = policy_checkpoint_path(settings, run, step)
    try:
        model = _remote_python(settings, _REMOTE_MODEL, path, timeout=30)
    except SparkError as exc:
        raise SparkNotFound(f"No usable remote model: {run}/{step} ({exc})") from exc
    problems = dimension_problems(model.get("state_dim"), model.get("action_dim"))
    rename_map = model.get("rename_map")
    expected_sources = {"observation.images.scene", "observation.images.wrist"}
    if not isinstance(rename_map, dict) or not expected_sources.issubset(rename_map):
        problems.append("The checkpoint does not preserve the scene/wrist camera rename map.")
    return {
        **model,
        "run": run,
        "step": step,
        "remote": True,
        "source": path,
        "camera_map": {
            feature: source.removeprefix("observation.images.")
            for source, feature in (rename_map or {}).items()
        },
        "runnable": not problems,
        "problems": problems,
    }


def ensure_policy_side(settings: Settings, *, timeout: float = 120.0) -> dict[str, Any]:
    """Start or reuse the policy side job, extend it for this trial, and wait until ready."""
    snapshot = _queue_request(settings, "GET", "/api/queue")
    side = snapshot.get("side")
    if side is not None and side.get("kind") != "soarm-policy":
        raise SparkBusy(f"Another side job is already running: {side.get('kind', 'unknown')}")
    if side is None:
        side = _queue_request(
            settings, "POST", "/api/side", {"kind": "soarm-policy", "params": {}}
        )
    else:
        _queue_request(settings, "POST", "/api/side/extend", {"seconds": 300})

    deadline = time.monotonic() + timeout
    while not side.get("stream_ready"):
        if not side.get("live", True):
            raise SparkError("The remote policy server stopped before becoming ready")
        if time.monotonic() >= deadline:
            raise SparkError(f"The remote policy server was not ready within {timeout:g} seconds")
        time.sleep(0.25)
        side = _queue_request(settings, "GET", "/api/queue").get("side")
        # Older sparkq versions omit ``live`` from side_view(). If the side job
        # disappears altogether, that is still an unambiguous death signal and
        # must not be converted to an empty live-by-default object for 120 seconds.
        if side is None:
            raise SparkError("The remote policy server stopped before becoming ready")
    return side


def stop_policy_side(settings: Settings) -> None:
    """Stop only the side lane; sparkq resumes any preempted training itself."""
    snapshot = _queue_request(settings, "GET", "/api/queue")
    side = snapshot.get("side")
    if side is not None and side.get("kind") == "soarm-policy":
        _queue_request(settings, "DELETE", "/api/side")


def policy_tunnel_command(settings: Settings) -> list[str]:
    port = settings.remote_policy_port
    return [
        "ssh",
        *SSH_OPTIONS,
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=2",
        "-o",
        "ServerAliveCountMax=2",
        "-N",
        "-L",
        f"{port}:127.0.0.1:{port}",
        _target(settings),
    ]


def _remote_dataset_root(settings: Settings) -> str:
    # `~`를 남겨 두면 rsync는 셸을 거치므로 확장되지만 `python3 -` 인자로는 확장되지 않는다.
    # 양쪽에서 같은 경로를 가리키도록 여기서 한 번만 다듬는다.
    return settings.spark_dataset_root.rstrip("/")


def probe(settings: Settings) -> dict[str, Any]:
    """Spark에 닿는지, GPU와 디스크가 어떤 상태인지."""
    try:
        info = _remote_python(settings, _PROBE, timeout=45)
    except SparkError as error:
        return {"reachable": False, "error": str(error), "host": settings.spark_host}
    info["host"] = settings.spark_host
    info["user"] = settings.spark_user
    return info


def list_datasets(settings: Settings) -> list[dict[str, Any]]:
    """Spark에 올라가 있는 데이터셋 목록."""
    return _remote_python(settings, _LIST_DATASETS, _remote_dataset_root(settings), timeout=45)


# `lerobot-train`이 이 콘솔에서 띄울 수 있는 정책과 그 값들.
#
# 두 벌뿐인 이유는 이 팔에서 실제로 돌려 본 것이 둘이기 때문이다. ACT는 처음부터
# 배우므로 10만 스텝이 필요하고, SmolVLA는 이미 배운 것을 옮겨 오므로 2만 스텝이면
# 충분하다 — 대신 첫 실행은 HF에서 기반 모델을 내려받느라 오래 걸린다.
#
# **이 표는 Spark의 `~/sparkq/kinds/lerobot-train.json`의 presets와 같은 값이어야 한다.**
# 학습을 거는 정상 경로는 sparkq 큐이고 이 엔드포인트는 사람이 직접 여는 문이라, 한쪽만
# 고치면 어느 문으로 걸었느냐에 따라 다른 학습이 돈다. 그 차이는 50시간 뒤에야 드러난다.
# (맥 앱에도 같은 표가 한 벌 더 있었고, 그것은 지웠다.)
TRAINING_POLICIES: dict[str, dict[str, Any]] = {
    "act": {"flag": "--policy.type=act", "steps": 100_000, "batch_size": 64, "save_freq": 20_000},
    "smolvla": {
        "flag": "--policy.path=lerobot/smolvla_base",
        "steps": 20_000,
        "batch_size": 32,
        "save_freq": 5_000,
    },
}

#: `run` 이름에서 데이터셋 부분이 쓸 수 있는 길이. 전체가 `NAME_PATTERN`(80자) 안에
#: 남아야 `pull_checkpoint`가 그 실행의 체크포인트를 가져올 수 있다.
_RUN_DATASET_CHARS = 56


def run_name(dataset: str, policy: str, now: Any = None) -> str:
    """`<dataset>__<policy>__<시각>`.

    시각을 넣는 이유는 LeRobot이 `output_dir`이 이미 있으면 `FileExistsError`로
    거절하기 때문이다(`configs/train.py`). 예전의 `train-command`는 `outputs/<dataset>`을
    고정으로 썼고, 그래서 같은 데이터셋의 두 번째 학습은 **반드시** 실패했다. 그 실패는
    tmux 안에서 일어나므로 화면에는 "시작했다"만 남았다.
    """
    from datetime import datetime

    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M")
    return f"{dataset[:_RUN_DATASET_CHARS]}__{policy}__{stamp}"


#: 학습 실행 옆에 우리가 적는 것들이 사는 자리 이름. `<output_root>/.runs/<run>/`에
#: `train.log`와 `soarm_train.json`이 놓인다.
#:
#: **`output_dir` 안에 두면 안 된다.** LeRobot은 `output_dir`이 이미 있으면
#: `FileExistsError`로 거절하는데(`configs/train.py:259`), 로그를 그 안에 두려면 `tee`가
#: 쓸 폴더를 미리 만들어야 하고 그 `mkdir`이 곧 거절 조건이다. 한때 그렇게 만들어 둔
#: 적이 있고, 학습은 1초 만에 죽었다. 폴더를 만드는 것은 LeRobot 혼자여야 한다.
#:
#: 점으로 시작하므로 `<output_root>`를 훑는 목록에서 실행 이름으로 읽히지 않는다.
RUN_SIDE_DIR = ".runs"


def _run_side_dir(settings: Settings, run: str) -> str:
    return f"{settings.spark_output_root.rstrip('/')}/{RUN_SIDE_DIR}/{run}"


def _run_output_dir(settings: Settings, run: str) -> str:
    return f"{settings.spark_output_root.rstrip('/')}/{run}"


def train_shell_line(settings: Settings, dataset: str, policy: str, run: str) -> str:
    """tmux 안에서 도는 한 줄. 원격 셸이 이 문자열 하나를 받는다.

    `mkdir -p`가 만드는 것은 **옆자리**(`.runs/<run>`)뿐이다. `--output_dir`이 가리키는
    폴더는 손대지 않는다 — LeRobot이 스스로 만들고, 미리 있으면 거절한다.
    """
    values = TRAINING_POLICIES[policy]
    output = _run_output_dir(settings, run)
    side = _run_side_dir(settings, run)
    return (
        "source ~/venvs/lerobot/bin/activate && "
        f"mkdir -p {side} && "
        "lerobot-train "
        f"--dataset.repo_id={dataset} "
        f"--dataset.root={_remote_dataset_root(settings)}/{dataset} "
        f"{values['flag']} "
        "--policy.device=cuda "
        "--policy.push_to_hub=false "
        f"--steps={values['steps']} "
        f"--batch_size={values['batch_size']} "
        f"--save_freq={values['save_freq']} "
        f"--output_dir={output} "
        "--wandb.enable=false "
        f"2>&1 | tee {side}/train.log"
    )


def start_training(settings: Settings, dataset: str, policy: str) -> dict[str, Any]:
    """학습을 tmux 안에서 띄운다. 콘솔은 그 뒤로 진행만 읽는다.

    콘솔이 학습 프로세스를 직접 품지 않는 것이 중요하다. 학습은 몇 시간 돌고, 콘솔은
    `systemctl --user restart`로 다시 시작되는 서비스다. tmux 안에 있으면 콘솔이
    내려갔다 올라와도 학습은 그대로 돈다.
    """
    import shlex
    import time as _time

    if not NAME_PATTERN.match(dataset):
        raise DatasetError("Unknown dataset name")
    if policy not in TRAINING_POLICIES:
        raise DatasetError(f"Unknown policy type: expected one of {sorted(TRAINING_POLICIES)}")

    root = _remote_dataset_root(settings)
    # preflight는 이미 도는 학습을 tmux의 `train-*` 세션으로 판단한다. 그 이름 규칙을
    # 강제하는 것은 이쪽이 아니라 sparkq의 `_session_name`이다. 즉 이 검사가 무엇을 보는지는
    # 큐가 세션 이름을 어떻게 짓느냐에 달려 있다 — 어느 한쪽의 이름 규칙을 고치는 사람은
    # 반드시 반대쪽도 함께 보아야 한다.
    state = _remote_python(settings, _TRAIN_PREFLIGHT, f"{root}/{dataset}", timeout=45)
    if not state.get("dataset_present"):
        raise SparkNotFound(
            f"Dataset is not on the training machine: {dataset}. Push it first."
        )
    already = [str(name) for name in state.get("running") or []]
    if already:
        # 이 기계에는 GPU가 하나다. 두 학습이 동시에 돌면 둘 다 메모리에서 쫓겨나거나
        # 둘 다 느려진다. 무엇이 돌고 있는지를 문구에 적어, 사람이 그것을 멈출지
        # 기다릴지 고를 수 있게 한다.
        raise SparkBusy(f"Training is already running: {already[0]}")

    run = run_name(dataset, policy)
    values = TRAINING_POLICIES[policy]
    line = train_shell_line(settings, dataset, policy, run)
    _run(
        [
            "ssh",
            *SSH_OPTIONS,
            _target(settings),
            "tmux",
            "new",
            "-d",
            "-s",
            f"train-{run}",
            shlex.quote(line),
        ],
        timeout=60,
    )
    meta = json.dumps({
        "dataset": dataset,
        "policy": policy,
        "steps": values["steps"],
        "batch_size": values["batch_size"],
        "started_at": _time.time(),
    })
    _remote_python(
        settings,
        _WRITE_TRAIN_META.replace("__PAYLOAD__", json.dumps(meta)),
        # `output_dir`이 아니라 옆자리다. 그 안에 무엇이든 쓰면 LeRobot이 시작을 거절한다.
        _run_side_dir(settings, run),
        timeout=45,
    )
    return {"run": run, "dataset": dataset, "policy": policy, "steps": values["steps"]}


def stop_training(settings: Settings, run: str) -> dict[str, Any]:
    """도는 학습에 Ctrl-C를 보낸다. 체크포인트는 그대로 남는다.

    먼저 `send-keys C-c`인 이유는 그것이 사람이 tmux에 붙어 눌렀을 때와 같은 길이기
    때문이다 — LeRobot은 그 신호를 받고 정리한 뒤 나간다. 그래도 살아 있으면 세션을
    죽인다. 어느 쪽이든 이미 디스크에 쓰인 체크포인트는 건드리지 않는다.
    """
    import time as _time

    if not NAME_PATTERN.match(run):
        raise DatasetError("Unknown run")
    session = f"train-{run}"
    target = _target(settings)
    try:
        _run(["ssh", *SSH_OPTIONS, target, "tmux", "send-keys", "-t", session, "C-c"], timeout=30)
    except SparkError as error:
        raise SparkNotFound(f"No such training run: {run} ({error})") from error
    _time.sleep(2.0)
    alive = subprocess.run(
        ["ssh", *SSH_OPTIONS, target, "tmux", "has-session", "-t", session],
        capture_output=True,
        text=True,
    )
    killed = False
    if alive.returncode == 0:
        _run(["ssh", *SSH_OPTIONS, target, "tmux", "kill-session", "-t", session], timeout=30)
        killed = True
    return {"run": run, "killed": killed}


def _local_dataset_dir(name: str):
    if not NAME_PATTERN.match(name):
        raise DatasetError("Unknown dataset name")
    directory = (data_root() / name).resolve()
    if not directory.is_relative_to(data_root().resolve()):
        raise DatasetError("Unknown dataset name")
    if not (directory / "meta/info.json").exists():
        raise FileNotFoundError(name)
    return directory


def push_dataset(settings: Settings, name: str, *, timeout: float = 3600) -> dict[str, Any]:
    """로컬 데이터셋 하나를 Spark로 보낸다.

    `--delete`를 쓰지 않는다. 녹화가 끝난 데이터셋은 더 늘지 줄지 않으므로 지울 것이 없고,
    실수로 원격의 다른 것을 지우는 쪽이 훨씬 비싸다.
    """
    directory = _local_dataset_dir(name)
    root = _remote_dataset_root(settings)
    target = _target(settings)
    # 받는 자리를 최종 위치가 아니라 `.incoming` 아래에 둔다.
    #
    # 전송이 끊기면 — 터널이 끊기거나, 디스크가 차거나, 사람이 앱을 닫으면 — 최종 위치에
    # 바로 받고 있었을 경우 `meta/info.json`만 도착한 디렉터리가 남는다. 목록은 그것을
    # 에피소드 수까지 갖춘 멀쩡한 데이터셋으로 읽고, 화면은 `전송됨`이라 말하며, 학습은
    # 영상이 없어 실패한다. 다 받은 뒤에 옮기면 이 상태가 아예 생기지 않는다.
    #
    # `.incoming`은 점으로 시작해 목록에 걸리지 않고, `--partial`이 남긴 조각이 그 안에
    # 남아 있으므로 다시 보낼 때 이어받는다.
    staging = f"{root}/.incoming/{name}"
    _run(["ssh", *SSH_OPTIONS, target, "mkdir", "-p", staging], timeout=30)
    raw = _run(
        [
            "rsync",
            "-a",
            "--partial",
            "--stats",
            "-e",
            "ssh " + " ".join(SSH_OPTIONS),
            f"{directory}/",
            f"{target}:{staging}/",
        ],
        timeout=timeout,
    )
    # 다 받았다. 이제 자리를 바꾼다. 지우고 옮기는 사이의 짧은 순간에는 데이터셋이 보이지
    # 않지만, 반쯤 받은 것이 멀쩡한 척 보이는 것보다는 낫다.
    _run(["ssh", *SSH_OPTIONS, target, "rm", "-rf", f"{root}/{name}"], timeout=60)
    _run(["ssh", *SSH_OPTIONS, target, "mv", staging, f"{root}/{name}"], timeout=60)
    return {"name": name, "remote": f"{root}/{name}", "stats": raw.strip()}


def pull_checkpoint(
    settings: Settings,
    run: str,
    step: str,
    *,
    timeout: float = 1800,
) -> dict[str, Any]:
    """학습된 체크포인트의 `pretrained_model`을 로컬로 가져온다.

    `training_state`는 가져오지 않는다. 추론에 쓰이지 않는데 optimizer 상태까지 있어
    훨씬 크고, 재개는 Spark에서 하는 것이 맞다.
    """
    if not NAME_PATTERN.match(run) or not NAME_PATTERN.match(step):
        raise DatasetError("Unknown run or step")
    destination = model_dir(run, step) / "pretrained_model"
    if destination.is_symlink():
        raise DatasetError("Model paths may not be symbolic links")
    destination.mkdir(parents=True, exist_ok=True)
    remote = (
        f"{_target(settings)}:{settings.spark_output_root}/{run}"
        f"/checkpoints/{step}/pretrained_model/"
    )
    _run(
        [
            "rsync",
            "-a",
            "--partial",
            "-e",
            "ssh " + " ".join(SSH_OPTIONS),
            remote,
            f"{destination}/",
        ],
        timeout=timeout,
    )
    return {"run": run, "step": step, "path": str(destination)}


# `lerobot-train --policy.type`이 받는 값. 목록을 여기 두는 이유는 아래에서 명령 문자열에
# 그대로 들어가기 때문이다 — 사람이 복사해 셸에 붙여 넣는 문자열이므로, 서버가 실행하지
# 않더라도 검증하지 않은 값이 섞이면 안 된다.
POLICY_TYPES = frozenset(
    {"act", "diffusion", "groot", "pi0", "pi0_fast", "pi05", "smolvla", "tdmpc", "vqbet"}
)


def train_command(settings: Settings, name: str, *, policy: str = "act", steps: int = 100_000,
                  batch_size: int = 64) -> str:
    """Spark에서 그대로 붙여 넣어 실행할 학습 명령.

    콘솔이 학습을 직접 띄우지는 않는다. 학습은 몇 시간 도는 작업이라 웹 요청의 수명과
    맞지 않고, 중간에 콘솔이 죽으면 학습도 같이 죽는다. 명령을 보여 주고 사람이 tmux에서
    시작하게 하는 편이 지금 단계에서는 더 정직하다.

    돌려주는 것이 셸 명령이므로 인자를 그대로 넣지 않는다. 이 문자열은 사람이 복사해
    실행하며, 그때는 이름에 섞인 따옴표 하나가 곧 임의 명령이 된다.
    """
    if not NAME_PATTERN.match(name):
        raise DatasetError("Unknown dataset name")
    if policy not in POLICY_TYPES:
        raise DatasetError("Unknown policy type")
    if not 1 <= steps <= 10_000_000 or not 1 <= batch_size <= 1024:
        raise DatasetError("steps or batch_size out of range")
    remote = f"{_remote_dataset_root(settings)}/{name}"
    return (
        f"ssh {settings.spark_user}@{settings.spark_host} -t "
        f"'tmux new -As train-{name} \""
        f"source ~/venvs/lerobot/bin/activate && "
        f"lerobot-train "
        f"--dataset.repo_id={name} "
        f"--dataset.root={remote} "
        f"--policy.type={policy} "
        f"--policy.device=cuda "
        f"--policy.push_to_hub=false "
        f"--steps={steps} "
        f"--batch_size={batch_size} "
        f"--output_dir={settings.spark_output_root}/{name} "
        f"--wandb.enable=false\"'"
    )
