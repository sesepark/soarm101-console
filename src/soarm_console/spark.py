from __future__ import annotations

import json
import subprocess
from typing import Any

from .config import Settings
from .datasets import NAME_PATTERN, DatasetError, data_root


class SparkError(RuntimeError):
    pass


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

# 학습 산출물은 `<output_root>/<run>/checkpoints/<step>/pretrained_model`에 쌓인다.
# 추론에 필요한 것은 `pretrained_model` 하나뿐이고 `training_state`는 재개용이라
# 목록에서 크기를 따로 알려 준다 — 회수할 때 무엇을 가져올지 화면에서 고르게 하려는 것이다.
_LIST_RUNS = """
import json, os, sys
root = os.path.expanduser(sys.argv[1])
out = []
if os.path.isdir(root):
    for run in sorted(os.listdir(root)):
        ckpt_root = os.path.join(root, run, "checkpoints")
        if not os.path.isdir(ckpt_root):
            continue
        steps = []
        for step in sorted(os.listdir(ckpt_root)):
            model = os.path.join(ckpt_root, step, "pretrained_model")
            if not os.path.isdir(model):
                continue
            size = 0
            for base, _dirs, files in os.walk(model):
                for name in files:
                    try:
                        size += os.path.getsize(os.path.join(base, name))
                    except OSError:
                        pass
            steps.append({
                "step": step,
                "size_bytes": size,
                "finished_at": os.path.getmtime(model),
            })
        if steps:
            out.append({"run": run, "checkpoints": steps})
print(json.dumps(out))
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


def list_runs(settings: Settings) -> list[dict[str, Any]]:
    """Spark의 학습 실행별 체크포인트 목록."""
    return _remote_python(settings, _LIST_RUNS, settings.spark_output_root, timeout=60)


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
    destination = data_root().parent / "checkpoints" / run / step
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
