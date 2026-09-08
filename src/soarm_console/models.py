from __future__ import annotations

import functools
import importlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .datasets import NAME_PATTERN, DatasetError


MANIFEST_NAME = "soarm_model.json"
CAMERA_ROLES = ("scene", "wrist")

#: SO-ARM101 has six joints.  Every width judged below is judged against this.
JOINTS = 6

#: Room a rollout needs on the GPU beyond the weights themselves: the CUDA context,
#: the image preprocessing and one forward pass of activations.  Generous on purpose
#: — a checkpoint that only just fits is a rollout that dies in the middle of a motion,
#: with the arm wherever it happened to be.
INFERENCE_HEADROOM = 1024 * 1024 * 1024


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


@functools.cache
def local_accelerator() -> tuple[int, float] | None:
    """``(GPU memory in bytes, compute capability)`` of this machine, or ``None``.

    Read through ``nvidia-smi`` rather than torch on purpose.  ``describe_model``
    runs on every listing the app polls, and importing torch into the API worker to
    learn one number would cost more memory than some of the checkpoints it is being
    asked about.  ``None`` means "could not tell", and every caller treats that as
    "do not object" — a missing driver must not invent problems.
    """
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total,compute_cap",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = completed.stdout.strip().splitlines()
    if not lines:
        return None
    memory, _, capability = lines[0].partition(",")
    try:
        return int(memory.strip()) * 1024 * 1024, float(capability.strip())
    except ValueError:
        return None


def local_weight_budget() -> int:
    """Largest checkpoint this machine can host locally, in bytes.  ``0`` means unknown.

    The app reads this before offering to pull a checkpoint down.  Without it the
    console happily fetched 9.35 GB of PI0.5 weights onto a box whose GPU holds 6 GB,
    and the download was pure waste of a home network.
    """
    accelerator = local_accelerator()
    if accelerator is None:
        return 0
    return max(0, accelerator[0] - INFERENCE_HEADROOM)


def _gigabytes(count: object) -> str:
    if not isinstance(count, (int, float)):
        return "an unknown amount"
    return f"{count / 1024 ** 3:.1f} GB"


def dimension_problems(state_dim: object, action_dim: object) -> list[str]:
    """Judge the widths a checkpoint declares against this rig's six joints.

    The two widths are not the same kind of claim.  **The state width is a ceiling.**
    PI0 and PI0.5 inherit ``observation.state`` of width 32 from ``lerobot/pi05_base``
    because openpi fits every robot it supports into one 32-slot vector, and PI0.5
    never projects the state through a layer of its own — it discretises it into the
    text prompt — so a six-wide state passes through a checkpoint whose config says 32.
    The normalization statistics saved beside the weights carry the honest width, and
    for our runs they are six.  Only a ceiling *below* six cannot carry this arm.

    **The action width is exact.**  Every number the head produces is written to a
    servo, so a head that emits some other count is driving a different robot.

    This judgement lives here alone because it is made twice — once for a checkpoint
    pulled onto the arm and once for one left on Spark.  It was written out twice for
    a day, and only the Spark copy learned about PI0.5; the arm copy went on rejecting
    every π0.5 checkpoint over a number it had misread.
    """
    problems: list[str] = []
    if not isinstance(state_dim, int) or state_dim < JOINTS:
        problems.append(
            f"The model state dimension must accommodate {JOINTS} joints, not {state_dim!r}."
        )
    if action_dim != JOINTS:
        problems.append(f"The model action dimension must be {JOINTS}, not {action_dim!r}.")
    return problems


def hosting_problem(manifest: dict[str, Any]) -> str | None:
    """Whether this machine can actually hold a checkpoint that was pulled onto it.

    A local rollout computes where the arm is, so weights that do not fit in this
    box's GPU cannot run here however sound the checkpoint itself is.  Saying that in
    the list beats letting the rollout start and die on an allocation halfway through
    a motion, and it names the way out: the same checkpoint runs on Spark without
    moving a byte.
    """
    accelerator = local_accelerator()
    if accelerator is None:
        return None
    memory, capability = accelerator
    weights = manifest.get("bytes")
    if isinstance(weights, int) and weights > memory - INFERENCE_HEADROOM:
        return (
            "This machine's GPU cannot hold the weights: "
            f"{_gigabytes(weights)} of weights, {_gigabytes(memory)} of GPU memory"
        )
    # bfloat16 needs Ampere.  Older cards load such weights only by converting them,
    # which doubles the memory the check above just measured.
    if manifest.get("dtype") == "bfloat16" and capability < 8.0:
        return (
            "This machine's GPU is too old for the checkpoint's bfloat16 weights: "
            f"compute capability {capability:g}, needs 8.0"
        )
    return None


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
        # Which numeric type the weights were saved as.  Old checkpoints pulled before
        # this field existed leave it ``None``, and the hosting check then says nothing
        # rather than guessing.
        "dtype": config.get("dtype"),
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


def camera_map(image_features: object, rename_map: object = None) -> dict[str, str]:
    """Which rig camera feeds each of the policy's image inputs.  ``{policy key: role}``.

    Both rollout paths ask this question — the copy pulled onto the arm and the one left on
    Spark — and for a day they answered it with different rules.  The remote path demanded a
    non-empty rename map and refused without one; the local path ignored the map entirely and
    zipped by position.  A GR00T checkpoint is fine by the second rule and rejected by the
    first, so the same weights ran on the arm and were blocked on Spark (2026-09-08).  One
    function now, three grounds, strongest first.

    **1. The checkpoint's own rename map.**  Policies whose config hard-codes camera names
    (SmolVLA's ``camera1/2/3``, PI0.5's ``base_0_rgb`` …) were trained with a map from this
    rig's keys to those names, and it is saved beside the weights.  Inverted, it is the
    authoritative answer.

    **2. The feature name itself.**  GR00T reads its camera keys from the dataset instead of a
    hard-coded config, so its inputs already *are* ``observation.images.scene``/``wrist`` and
    its saved map is an empty dict — there was nothing to rename.  Matching by name is exact.

    **3. Position, last.**  ``zip`` silently swaps scene and wrist if ``input_features`` ever
    comes back in the other order, so it is the fallback for names we cannot recognise, not
    the first rule.  Only features left over after 1 and 2 reach it, and only roles left over
    are handed out.
    """
    # An absent feature list is not an absent answer: the saved rename map alone can say which
    # rig camera became which policy input, and grounds 2 and 3 simply have nothing to work
    # with.  Returning early here made the map unreachable for exactly those checkpoints.
    names = [name for name in image_features if isinstance(name, str)] if isinstance(
        image_features, list
    ) else []
    mapping: dict[str, str] = {}

    if isinstance(rename_map, dict):
        for source, feature in rename_map.items():
            if not isinstance(source, str) or not isinstance(feature, str):
                continue
            role = source.removeprefix("observation.images.")
            # The map is authoritative on its own — it is not cross-checked against
            # ``input_features``.  GR00T shows why the two can disagree: it never reads its
            # camera keys from the config at all.  Requiring the target to also appear in the
            # feature list would refuse a checkpoint whose map is the only honest record of
            # what the policy consumes.
            if role in CAMERA_ROLES:
                mapping[feature] = role

    for name in names:
        role = name.removeprefix("observation.images.")
        if name not in mapping and role in CAMERA_ROLES and role not in mapping.values():
            mapping[name] = role

    spare = [role for role in CAMERA_ROLES if role not in mapping.values()]
    for name in names:
        if name in mapping or not spare:
            continue
        mapping[name] = spare.pop(0)
    return mapping


def _policy_problem(policy: object) -> str | None:
    if not isinstance(policy, str) or not policy:
        return "The model does not identify its policy type."
    try:
        importlib.import_module(f"lerobot.policies.{policy}.configuration_{policy}")
        from lerobot.policies.factory import get_policy_class

        get_policy_class(policy)
    except Exception as exc:  # optional policy dependencies fail in several import-time forms
        return f"Policy type '{policy}' is not supported by this installation: {exc}"
    return None


def describe_model(run: str, step: str) -> dict[str, Any]:
    directory = model_dir(run, step, must_exist=True)
    manifest = _json_object(directory / MANIFEST_NAME)
    problems: list[str] = []
    pretrained = directory / "pretrained_model"
    if pretrained.is_symlink() or not pretrained.is_dir():
        problems.append("The pretrained_model directory is missing.")
    problems.extend(dimension_problems(manifest.get("state_dim"), manifest.get("action_dim")))
    mapping = camera_map(manifest.get("image_features"))
    if not mapping:
        problems.append("None of the model's image features can be mapped to this rig's cameras.")
    policy_problem = _policy_problem(manifest.get("policy"))
    if policy_problem:
        problems.append(policy_problem)
    # Last, because it is the one problem that is about this machine rather than about
    # the checkpoint: the same weights are fine, they just have to run on Spark.
    hosting = hosting_problem(manifest)
    if hosting:
        problems.append(hosting)
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
