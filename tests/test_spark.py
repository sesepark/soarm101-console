"""학습 서버로 가는 길. 원격 기계 없이 도는 시험만 여기에 둔다.

원격에서 도는 파이썬 스크립트는 문자열이므로, 이 파일은 그것을 **로컬 인터프리터로
실제로 실행해** 본다. 문자열을 눈으로 읽어 넘기면 원격에서만 나는 오류가 생긴다.
"""

from __future__ import annotations

import json
import os
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


def _list_runs(root) -> list:
    return _run_remote_script(spark._LIST_RUNS, str(root), spark.RUN_SIDE_DIR)


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


# MARK: 진행 읽기 — 원격 스크립트를 실제로 돌린다


def _make_run(root, name: str, *, steps: list[str] = (), log: str = "", meta: dict | None = None):
    """실행 하나를 두 자리에 나누어 만든다.

    LeRobot이 만드는 `<root>/<run>/checkpoints/`와, 우리가 만드는
    `<root>/.runs/<run>/`(로그와 메타)이다. `steps`만 주면 손으로 돌린 옛 학습이 되고,
    `meta`만 주면 방금 시작해 아직 체크포인트가 없는 학습이 된다.
    """
    if steps:
        checkpoints = root / name / "checkpoints"
        checkpoints.mkdir(parents=True)
        for step in steps:
            (checkpoints / step / "pretrained_model").mkdir(parents=True)
            (checkpoints / step / "pretrained_model" / "model.safetensors").write_bytes(b"weights")
        # LeRobot은 가장 최근 체크포인트를 가리키는 `last` 심볼릭 링크를 남긴다.
        os.symlink(checkpoints / steps[-1], checkpoints / "last")
    side = root / spark.RUN_SIDE_DIR / name
    if log or meta is not None:
        side.mkdir(parents=True, exist_ok=True)
    if log:
        (side / "train.log").write_text(log, encoding="utf-8")
    if meta is not None:
        (side / "soarm_train.json").write_text(json.dumps(meta), encoding="utf-8")
    return root / name


def test_list_runs_does_not_count_the_last_symlink_as_another_checkpoint(tmp_path):
    """`checkpoints/last`를 따라가면 같은 체크포인트가 목록에 두 번 나온다.

    실제로 그랬다 — 화면이 있지도 않은 회수를 세고, 회수하는 쪽은 어느 것을 가져올지
    고를 수 없었다.
    """
    _make_run(tmp_path, "pick__act__20260905_1830", steps=["020000", "040000"])

    (run,) = _list_runs(tmp_path)

    assert [checkpoint["step"] for checkpoint in run["checkpoints"]] == ["020000", "040000"]
    assert run["training"] is None


def test_list_runs_reads_the_step_and_loss_out_of_the_training_log(tmp_path):
    """LeRobot은 step을 `format_big_number`로 줄여 찍는다 — `step:20K`.

    숫자만 집으면 20이 되고, 화면은 10만 스텝 학습이 0.02% 진행됐다고 말한다.
    """
    _make_run(
        tmp_path,
        "pick__act__20260905_1830",
        steps=["020000"],
        log=(
            "INFO 2026-09-05 18:30:00 step:10K smpl:640K ep:80 epch:8.00 loss:0.412\n"
            "INFO 2026-09-05 18:40:00 step:20K smpl:1M ep:160 epch:16.00 loss:0.187\n"
        ),
        meta={"dataset": "pick", "policy": "act", "steps": 100000, "batch_size": 64,
              "started_at": 1.0},
    )

    (run,) = _list_runs(tmp_path)
    training = run["training"]

    assert training["step"] == 20_000
    assert training["steps"] == 100_000
    assert training["loss"] == pytest.approx(0.187)
    assert training["policy"] == "act"
    assert training["log_tail"][-1].endswith("loss:0.187")
    assert training["updated_at"] > 0


def test_progress_is_readable_before_the_first_metric_line(tmp_path):
    """LeRobot은 `step:N` 줄을 `log_freq`(기본 200)마다 찍는다.

    이 팔의 ACT 학습은 스텝당 2초가 넘으므로 그 첫 줄이 **8분 뒤에** 나온다. 그동안
    화면은 학습이 도는 것은 아는데 어디까지 갔는지 모른다 — 실제로 방금 띄운 학습이
    30초 동안 `step: null`이었다. tqdm의 막대는 매 스텝 갱신하므로 그것을 읽는다.
    """
    _make_run(
        tmp_path,
        "pick__act__20260905_1830",
        log=(
            "INFO Start offline training on a fixed dataset\n"
            "Training:   0%|          | 10/100000 [00:35<95:45:43,  3.45s/step]\n"
        ),
        meta={"policy": "act", "steps": 100000},
    )

    (run,) = _list_runs(tmp_path)

    assert run["training"]["step"] == 10
    # loss는 tqdm이 나르지 않는다. 없는 것을 지어내지 않는다.
    assert run["training"]["loss"] is None


def test_the_metric_line_and_the_progress_bar_count_the_same_thing(tmp_path):
    """둘 다 있으면 더 최근인 쪽 — 늘 tqdm이다."""
    _make_run(
        tmp_path,
        "pick__act__20260905_1830",
        log=(
            "INFO step:20K smpl:1M ep:160 epch:16.00 loss:0.187\n"
            "Training:  20%|##        | 20147/100000 [11:00<43:00:00,  1.94s/step]\n"
        ),
        meta={"policy": "act", "steps": 100000},
    )

    training = _list_runs(tmp_path)[0]["training"]

    assert training["step"] == 20147
    # 그래도 loss는 metric 줄에서 온다.
    assert training["loss"] == pytest.approx(0.187)


def test_list_runs_says_why_a_run_that_is_not_running_stopped_early(tmp_path):
    _make_run(
        tmp_path,
        "pick__act__20260905_1830",
        log=(
            "INFO step:1K loss:0.9\n"
            "Traceback (most recent call last):\n"
            "torch.OutOfMemoryError: CUDA out of memory\n"
        ),
        meta={"policy": "act", "steps": 100000},
    )

    (run,) = _list_runs(tmp_path)
    training = run["training"]

    # tmux 세션이 없고 스텝이 모자라다 — 로그에서 이유가 될 만한 마지막 줄을 집는다.
    assert training["running"] is False
    assert "CUDA out of memory" in training["error"]


def test_list_runs_shows_a_run_before_its_first_checkpoint_exists(tmp_path):
    """ACT의 첫 저장은 2만 스텝 뒤다. 그때까지 목록에서 사라져 있으면 앱이 진행을 그릴
    수 없고, 사람은 학습이 시작됐는지조차 알 수 없다.

    이 실행은 `<root>/<run>/`이 아예 없다 — LeRobot이 `output_dir`을 만들기 전이거나
    만드는 중이고, 우리가 만든 것은 `.runs/<run>/`뿐이다.
    """
    _make_run(
        tmp_path,
        "pick__act__20260905_1830",
        log="INFO step:500 loss:1.2\n",
        meta={"policy": "act", "steps": 100000},
    )
    assert not (tmp_path / "pick__act__20260905_1830").exists()

    (run,) = _list_runs(tmp_path)

    assert run["run"] == "pick__act__20260905_1830"
    assert run["checkpoints"] == []
    assert run["training"]["step"] == 500


def test_list_runs_is_the_union_of_the_two_places_a_run_lives(tmp_path):
    """체크포인트만 있는 실행과 옆자리만 있는 실행이 함께 나와야 한다.

    손으로 돌린 옛 학습에는 `soarm_train.json`이 없고, 방금 시작한 학습에는 체크포인트가
    없다. 어느 한쪽만 훑으면 회수할 것을 못 찾거나 진행을 못 그린다.
    """
    _make_run(tmp_path, "old_by_hand", steps=["020000"])
    _make_run(tmp_path, "just_started", meta={"policy": "act", "steps": 100000})

    listed = _list_runs(tmp_path)

    assert [run["run"] for run in listed] == ["just_started", "old_by_hand"]
    assert listed[0]["checkpoints"] == []
    assert listed[0]["training"]["steps"] == 100000
    assert [c["step"] for c in listed[1]["checkpoints"]] == ["020000"]
    assert listed[1]["training"] is None


def test_an_empty_leftover_folder_is_not_counted_as_a_run(tmp_path):
    """남아 있는 빈 폴더 하나가 실행 하나로 세어지면 안 된다."""
    (tmp_path / "leftover").mkdir()
    (tmp_path / spark.RUN_SIDE_DIR / "half_written").mkdir(parents=True)

    assert _list_runs(tmp_path) == []


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
