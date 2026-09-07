"""학습 서버로 가는 길. 원격 기계 없이 도는 시험만 여기에 둔다.

원격에서 도는 파이썬 스크립트는 문자열이므로, 이 파일은 그것을 **로컬 인터프리터로
실제로 실행해** 본다. 문자열을 눈으로 읽어 넘기면 원격에서만 나는 오류가 생긴다.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime

import pytest

from soarm_console import spark
from soarm_console.config import Settings
from soarm_console.datasets import DatasetError


def _settings(**overrides) -> Settings:
    base = {
        "spark_host": "spark.example",
        "spark_user": "operator",
        "spark_dataset_root": "data/soarm",
        "spark_output_root": "outputs",
    }
    base.update(overrides)
    return Settings(**base)


def _run_remote_script(script: str, *argv: str) -> object:
    """원격에서 `python3 -`가 하는 일을 여기서 그대로 한다."""
    result = subprocess.run(
        [sys.executable, "-", *argv], input=script, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# MARK: 학습 시작


def test_start_training_names_the_run_after_the_dataset_policy_and_time():
    run = spark.run_name("pick_place_20260905_1820", "act", datetime(2026, 9, 5, 18, 30))
    assert run == "pick_place_20260905_1820__act__20260905_1830"
    # 시각이 들어가는 이유는 LeRobot이 `output_dir`이 이미 있으면 `FileExistsError`로
    # 거절하기 때문이다. 고정 이름이면 같은 데이터셋의 두 번째 학습이 반드시 실패한다.
    later = spark.run_name("pick_place_20260905_1820", "act", datetime(2026, 9, 5, 19, 0))
    assert later != run


def test_the_run_name_stays_inside_the_pattern_that_pulls_its_checkpoints():
    from soarm_console.datasets import NAME_PATTERN

    run = spark.run_name("x" * 80, "smolvla", datetime(2026, 9, 5, 18, 30))
    assert NAME_PATTERN.match(run), run


def test_start_training_hands_ssh_an_argument_list_with_no_shell_in_the_middle(monkeypatch):
    """원격 명령은 인자 리스트로만 만든다.

    데이터셋 이름이 그대로 원격 경로가 되므로, 이 경로 어딘가에서 셸이 한 번이라도
    문자열을 다시 가르면 이름 하나로 임의 명령이 실행된다. tmux에 넘기는 한 줄은
    `shlex.quote`로 통째로 감싸 **원격 셸이 한 덩어리로 읽게** 한다.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(
        spark,
        "_remote_python",
        lambda settings, script, *argv, timeout=60: (
            {"dataset_present": True, "running": []} if "dataset_present" in script else {"ok": True}
        ),
    )
    monkeypatch.setattr(
        spark, "_run", lambda args, timeout, stdin=None: calls.append(args) or ""
    )

    result = spark.start_training(_settings(), "pick_place_20260905_1820", "act")

    assert result["run"].startswith("pick_place_20260905_1820__act__")
    assert result["steps"] == 100_000
    (args,) = calls
    assert args[0] == "ssh"
    assert args[-6:-1] == ["tmux", "new", "-d", "-s", f"train-{result['run']}"]
    # 마지막 인자가 tmux가 받을 한 줄이고, 통째로 따옴표 안에 들어 있다.
    line = args[-1]
    assert line.startswith("'") and line.endswith("'")
    assert "lerobot-train" in line
    assert "--policy.type=act" in line
    assert "--save_freq=20000" in line
    assert "--output_dir=outputs/" in line
    # 따옴표 바깥에 셸 메타문자가 남아 있으면 안 된다. 안쪽은 원격 셸이 한 덩어리로 읽는다.
    assert all(
        character not in argument
        for argument in args[:-1]
        for character in ";|&$`\n"
    )


def test_smolvla_trains_for_fewer_steps_with_a_pretrained_base():
    run = "d__smolvla__20260905_1830"
    line = spark.train_shell_line(_settings(), "d", "smolvla", run)
    assert "--policy.path=lerobot/smolvla_base" in line
    assert "--policy.type=" not in line
    assert "--steps=20000" in line
    assert "--batch_size=32" in line
    assert "--save_freq=5000" in line
    # 로그는 tee로 남긴다. 진행을 읽는 유일한 길이다.
    assert line.endswith(f"2>&1 | tee outputs/.runs/{run}/train.log")


def test_nothing_we_write_lands_inside_the_directory_lerobot_wants_to_create():
    """LeRobot은 `output_dir`이 이미 있으면 `FileExistsError`로 거절한다
    (`configs/train.py:259`).

    한때 로그를 `output_dir` 안에 두려고 `mkdir -p <output_dir>`을 앞에 붙였다. 그
    `mkdir`이 곧 거절 조건이라 **학습이 1초 만에 죽었고**, 죽은 자리가 tmux 안이라
    화면에는 "시작했다"만 남았다. 폴더를 만드는 것은 LeRobot 혼자여야 한다.
    """
    run = "d__act__20260905_1830"
    settings = _settings()
    output = spark._run_output_dir(settings, run)
    line = spark.train_shell_line(settings, "d", "act", run)

    assert f"--output_dir={output} " in line
    # `mkdir -p`의 대상은 옆자리 하나뿐이고, 그것은 `output_dir` 밑에 있지 않다.
    (mkdir_target,) = [
        part.removeprefix("mkdir -p ")
        for part in line.split(" && ")
        if part.startswith("mkdir -p ")
    ]
    assert mkdir_target == spark._run_side_dir(settings, run)
    assert not mkdir_target.startswith(output + "/")
    # `tee`가 쓰는 파일도 마찬가지다 — 그 파일을 만들려면 부모 폴더가 있어야 한다.
    tee_target = line.rsplit("| tee ", 1)[1].strip()
    assert not tee_target.startswith(output + "/")
    # 그리고 우리가 쓰는 어떤 경로도 `output_dir` 안을 가리키지 않는다.
    assert f"{output}/" not in line.replace(f"--output_dir={output} ", "")


def test_start_training_refuses_a_dataset_that_is_not_on_the_training_machine(monkeypatch):
    monkeypatch.setattr(
        spark,
        "_remote_python",
        lambda settings, script, *argv, timeout=60: {"dataset_present": False, "running": []},
    )
    with pytest.raises(spark.SparkNotFound) as error:
        spark.start_training(_settings(), "never_pushed", "act")
    assert "Dataset is not on the training machine" in str(error.value)


def test_start_training_refuses_while_another_run_holds_the_gpu(monkeypatch):
    monkeypatch.setattr(
        spark,
        "_remote_python",
        lambda settings, script, *argv, timeout=60: {
            "dataset_present": True,
            "running": ["other__act__20260905_1200"],
        },
    )
    with pytest.raises(spark.SparkBusy) as error:
        spark.start_training(_settings(), "pick", "act")
    # 무엇이 돌고 있는지를 적는다 — 사람이 그것을 멈출지 기다릴지 골라야 한다.
    assert "Training is already running: other__act__20260905_1200" in str(error.value)


def test_start_training_takes_only_the_two_policies_this_arm_has_run(monkeypatch):
    monkeypatch.setattr(
        spark,
        "_remote_python",
        lambda settings, script, *argv, timeout=60: {"dataset_present": True, "running": []},
    )
    with pytest.raises(DatasetError):
        spark.start_training(_settings(), "pick", "pi0")
    with pytest.raises(DatasetError):
        spark.start_training(_settings(), "../etc", "act")


def test_the_training_preflight_answers_both_questions_in_one_round_trip(tmp_path):
    (tmp_path / "pick" / "meta").mkdir(parents=True)
    (tmp_path / "pick" / "meta" / "info.json").write_text("{}", encoding="utf-8")

    present = _run_remote_script(spark._TRAIN_PREFLIGHT, str(tmp_path / "pick"))
    absent = _run_remote_script(spark._TRAIN_PREFLIGHT, str(tmp_path / "never"))

    assert present["dataset_present"] is True
    assert absent["dataset_present"] is False
    assert isinstance(present["running"], list)


def test_the_training_metadata_script_carries_quotes_without_an_escape_dance(tmp_path):
    """적을 내용은 인자가 아니라 스크립트 안에 넣는다.

    `_remote_python`의 argv는 ssh가 공백으로 이어 붙여 원격 셸이 다시 가르는 자리다.
    따옴표와 공백이 든 JSON은 그 길로 넘어가지 못한다.
    """
    payload = json.dumps({"dataset": "pick", "policy": "act", "steps": 100000})
    script = spark._WRITE_TRAIN_META.replace("__PAYLOAD__", json.dumps(payload))

    _run_remote_script(script, str(tmp_path / "run"))

    written = json.loads((tmp_path / "run" / "soarm_train.json").read_text(encoding="utf-8"))
    assert written == {"dataset": "pick", "policy": "act", "steps": 100000}


# MARK: remote policy inference


def test_remote_checkpoint_path_is_absolute_on_spark():
    settings = _settings(spark_home="/home/operator")

    assert spark.policy_checkpoint_path(settings, "pick__pi05__abcd", "002000") == (
        "/home/operator/outputs/pick__pi05__abcd/checkpoints/002000/pretrained_model"
    )


def test_remote_checkpoint_path_refuses_traversal():
    with pytest.raises(DatasetError):
        spark.policy_checkpoint_path(_settings(), "../outside", "002000")


def test_policy_side_is_extended_once_per_reused_trial(monkeypatch):
    calls = []
    ready = {"kind": "soarm-policy", "stream_ready": True, "live": True}

    def request(settings, method, path, payload=None):
        calls.append((method, path, payload))
        if path == "/api/queue":
            return {"side": ready}
        return {}

    monkeypatch.setattr(spark, "_queue_request", request)

    assert spark.ensure_policy_side(_settings()) == ready
    assert calls == [
        ("GET", "/api/queue", None),
        ("POST", "/api/side/extend", {"seconds": 300}),
    ]


def test_policy_side_refuses_to_replace_an_unrelated_side_job(monkeypatch):
    monkeypatch.setattr(
        spark,
        "_queue_request",
        lambda *args, **kwargs: {"side": {"kind": "isaac-play", "stream_ready": True}},
    )

    with pytest.raises(spark.SparkBusy, match="isaac-play"):
        spark.ensure_policy_side(_settings())


def test_remote_model_uses_the_checkpoint_saved_camera_map(monkeypatch):
    monkeypatch.setattr(
        spark,
        "_remote_python",
        lambda *args, **kwargs: {
            "policy": "pi05",
            "dataset": "pick",
            "state_dim": 32,
            "action_dim": 6,
            "rename_map": {
                "observation.images.scene": "observation.images.base_0_rgb",
                "observation.images.wrist": "observation.images.left_wrist_0_rgb",
            },
        },
    )

    model = spark.describe_remote_model(
        _settings(spark_home="/home/operator"), "pick__pi05__abcd", "002000"
    )

    assert model["runnable"] is True
    assert model["camera_map"] == {
        "observation.images.base_0_rgb": "scene",
        "observation.images.left_wrist_0_rgb": "wrist",
    }
