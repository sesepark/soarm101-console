from __future__ import annotations

import importlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .datasets import NAME_PATTERN, DatasetError


MANIFEST_NAME = "soarm_model.json"
CAMERA_ROLES = ("scene", "wrist")


class ModelNotFound(FileNotFoundError):
    pass


def models_root() -> Path:
    return Path(__file__).parents[2] / "models"


def model_dir(run: str, step: str, *, must_exist: bool = False) -> Path:
    """Return a model path only when every component stays inside ``models/``.

    The name check blocks ``..`` and slashes.  The symlink check is separate: a
    valid-looking name may still have been replaced with a link after a previous
    download, and model deletion must never follow it.
    """
    if not NAME_PATTERN.fullmatch(run) or not NAME_PATTERN.fullmatch(step):
        raise DatasetError("Unknown run or step")
    root = models_root().resolve()
    run_path = models_root() / run
    path = run_path / step
    if run_path.is_symlink() or path.is_symlink():
        raise DatasetError("Model paths may not be symbolic links")
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise DatasetError("Unknown run or step")
    if must_exist and not path.is_dir():
        raise ModelNotFound(f"No such model: {run}/{step}")
    return path


def _json_object(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise DatasetError(f"{path.name} may not be a symbolic link")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DatasetError(f"Cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise DatasetError(f"{path.name} must contain a JSON object")
    return value


def _shape_dimension(features: object, key: str) -> int | None:
    if not isinstance(features, dict):
        return None
    feature = features.get(key)
    if not isinstance(feature, dict):
        return None
    shape = feature.get("shape")
    if not isinstance(shape, list) or not shape or not isinstance(shape[0], int):
        return None
    return shape[0]


def _directory_bytes(directory: Path) -> int:
    total = 0
    for base, directories, files in os.walk(directory, followlinks=False):
        # A downloaded checkpoint is data, not a place from which links may escape.
        directories[:] = [name for name in directories if not (Path(base) / name).is_symlink()]
        for name in files:
            path = Path(base) / name
            if path.is_symlink():
                continue
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return total


def build_manifest(
    settings: Settings, run: str, step: str, *, pulled_at: float | None = None
) -> dict[str, Any]:
    """Build the local manifest solely from files received with the checkpoint."""
    directory = model_dir(run, step, must_exist=True)
    pretrained = directory / "pretrained_model"
    if pretrained.is_symlink() or not pretrained.is_dir():
        raise DatasetError("The checkpoint has no pretrained_model directory")
    config = _json_object(pretrained / "config.json")
    train = _json_object(pretrained / "train_config.json")
    input_features = config.get("input_features")
    image_features = []
    if isinstance(input_features, dict):
        image_features = [
            name
            for name, feature in input_features.items()
            if isinstance(feature, dict)
            and (feature.get("type") == "VISUAL" or name.startswith("observation.images."))
        ]
    dataset = train.get("dataset")
    dataset_name = dataset.get("repo_id") if isinstance(dataset, dict) else None
    manifest = {
        "run": run,
        "step": step,
        "policy": config.get("type"),
        "dataset": dataset_name,
        "trained_steps": train.get("steps"),
        "chunk_size": config.get("chunk_size"),
        "n_action_steps": config.get("n_action_steps"),
        "image_features": image_features,
        "state_dim": _shape_dimension(input_features, "observation.state"),
        "action_dim": _shape_dimension(config.get("output_features"), "action"),
        "pulled_at": time.time() if pulled_at is None else pulled_at,
        "source": (
            f"{settings.spark_user}@{settings.spark_host}:"
            f"{settings.spark_output_root.rstrip('/')}/{run}/checkpoints/{step}/pretrained_model"
        ),
        "bytes": _directory_bytes(pretrained),
    }
    target = directory / MANIFEST_NAME
    temporary = target.with_suffix(".tmp")
    if temporary.is_symlink():
        raise DatasetError(f"{temporary.name} may not be a symbolic link")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return manifest


def camera_map(image_features: object) -> dict[str, str]:
    if not isinstance(image_features, list):
        return {}
    names = [name for name in image_features if isinstance(name, str)]
    return dict(zip(names, CAMERA_ROLES, strict=False))


def _policy_problem(policy: object) -> str | None:
    if not isinstance(policy, str) or not policy:
        return "The model does not identify its policy type."
    try:
        importlib.import_module(f"lerobot.policies.{policy}.configuration_{policy}")
        from lerobot.policies.factory import get_policy_class
        from lerobot.policies.pretrained import PreTrainedPolicy

        policy_class = get_policy_class(policy)
    except Exception as exc:  # optional policy dependencies fail in several import-time forms
        return f"Policy type '{policy}' is not supported by this installation: {exc}"
    if policy_class.supports_rtc is PreTrainedPolicy.supports_rtc:
        return f"Policy type '{policy}' does not support RTC inference in this LeRobot installation."
    return None


def describe_model(run: str, step: str) -> dict[str, Any]:
    directory = model_dir(run, step, must_exist=True)
    manifest = _json_object(directory / MANIFEST_NAME)
    problems: list[str] = []
    pretrained = directory / "pretrained_model"
    if pretrained.is_symlink() or not pretrained.is_dir():
        problems.append("The pretrained_model directory is missing.")
    if manifest.get("state_dim") != 6:
        problems.append(f"The model state dimension must be 6, not {manifest.get('state_dim')!r}.")
    if manifest.get("action_dim") != 6:
        problems.append(f"The model action dimension must be 6, not {manifest.get('action_dim')!r}.")
    mapping = camera_map(manifest.get("image_features"))
    if not mapping:
        problems.append("None of the model's image features can be mapped to this rig's cameras.")
    policy_problem = _policy_problem(manifest.get("policy"))
    if policy_problem:
        problems.append(policy_problem)
    return {**manifest, "camera_map": mapping, "runnable": not problems, "problems": problems}


def list_models() -> list[dict[str, Any]]:
    root = models_root()
    if not root.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for run_path in root.iterdir():
        if run_path.is_symlink() or not run_path.is_dir() or not NAME_PATTERN.fullmatch(run_path.name):
            continue
        for step_path in run_path.iterdir():
            if step_path.is_symlink() or not step_path.is_dir() or not NAME_PATTERN.fullmatch(step_path.name):
                continue
            try:
                found.append(describe_model(run_path.name, step_path.name))
            except (DatasetError, ModelNotFound):
                continue
    return sorted(found, key=lambda item: float(item.get("pulled_at") or 0), reverse=True)


def delete_model(run: str, step: str) -> dict[str, Any]:
    directory = model_dir(run, step, must_exist=True)
    freed = _directory_bytes(directory)
    shutil.rmtree(directory)
    try:
        directory.parent.rmdir()
    except OSError:
        pass
    return {"run": run, "step": step, "freed_bytes": freed}
