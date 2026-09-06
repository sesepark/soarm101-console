"""외부 캘리브레이션이 팔을 세울 자세들을 만든다.

**자세를 아무렇게나 고르면 안 되는 이유.** 손-눈 방정식의 회전 미지수는 자세 쌍의
**회전축**에서만 정보를 얻는다. 팔을 한 축으로만 돌리면 그 축 방향 성분이 방정식에
나타나지 않아 영원히 결정되지 않는다(축퇴). 그때 재투영 잔차는 작게 나오는데 답은
틀린다 — 실물에서 알아챌 방법이 거의 없는 가장 나쁜 실패다.

그래서 두 가지를 강제한다. 하나는 그리퍼가 **작업 영역 안**에 있을 것(카메라가 봐야 한다),
다른 하나는 새 자세의 회전이 이미 뽑은 자세들과 **충분히 다를 것**이다.
"""

from __future__ import annotations

import math

import numpy as np

from .kinematics import Chain


#: 그리퍼가 있어야 할 곳. 베이스에서의 수평 거리[m]와 높이[m].
#: 수직 접근이 유지되는 반지름은 시뮬 실측으로 118~278mm였다. 카메라가 보는 자리도
#: 그 근처이므로 같은 띠를 쓴다.
REACH_M = (0.12, 0.30)
HEIGHT_M = (0.05, 0.30)
#: 새 자세로 인정할 최소 회전 차이[도]. 이보다 가까우면 새 정보가 거의 없다.
MINIMUM_ROTATION_DEG = 12.0
#: 관절 범위의 이 비율 안에서만 뽑는다. 한계에 붙은 자세는 기구적으로 무리가 가고,
#: 중력 처짐도 커져 정기구학이 말하는 자리와 실제가 더 벌어진다.
RANGE_FRACTION = 0.55


def _rotation_angle(a: np.ndarray, b: np.ndarray) -> float:
    """두 회전 사이의 각도[도]. 측지 거리다."""
    relative = a[:3, :3].T @ b[:3, :3]
    cosine = (float(np.trace(relative)) - 1.0) / 2.0
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def generate(
    specs,
    chain: Chain,
    count: int,
    *,
    gripper: float,
    seed: int = 20260906,
    reach: tuple[float, float] = REACH_M,
    height: tuple[float, float] = HEIGHT_M,
) -> list[dict[str, float]]:
    """팔로워 단위(도·퍼센트)의 자세 목록.

    `gripper`는 시작할 때의 값을 그대로 유지한다. 캘리브레이션 도중에 집게를 여닫으면
    그리퍼 몸통에 붙인 마커가 흔들릴 수 있고, 그러면 외부 파라미터가 통째로 틀린다.
    """
    by_name = {spec.name: spec for spec in specs}
    movable = [name for name in chain.movable_names if name in by_name]
    rng = np.random.default_rng(seed)

    accepted: list[dict[str, float]] = []
    poses: list[np.ndarray] = []
    for _ in range(count * 400):
        if len(accepted) >= count:
            break
        candidate: dict[str, float] = {}
        for name in movable:
            spec = by_name[name]
            middle = (spec.minimum + spec.maximum) / 2.0
            half = spec.span / 2.0 * RANGE_FRACTION
            value = float(rng.uniform(middle - half, middle + half))
            if not spec.contains(value):
                break
            candidate[name] = value
        else:
            angles = {
                by_name[name].urdf_joint: by_name[name].to_radians(value)
                for name, value in candidate.items()
            }
            pose = chain.forward(angles)
            tip = pose[:3, 3]
            distance = float(math.hypot(tip[0], tip[1]))
            if not (reach[0] <= distance <= reach[1] and height[0] <= tip[2] <= height[1]):
                continue
            if any(_rotation_angle(pose, existing) < MINIMUM_ROTATION_DEG for existing in poses):
                continue
            poses.append(pose)
            candidate["gripper"] = float(gripper)
            accepted.append(candidate)
    return accepted


def rotation_spread(specs, chain: Chain, poses: list[dict[str, float]]) -> float:
    """뽑힌 자세들의 회전이 얼마나 넓게 퍼져 있는가[도]. 축퇴를 눈으로 보는 숫자다."""
    by_name = {spec.name: spec for spec in specs}
    matrices = []
    for pose in poses:
        angles = {
            by_name[name].urdf_joint: by_name[name].to_radians(value)
            for name, value in pose.items()
            if name in by_name and by_name[name].urdf_joint in chain.movable_names
        }
        matrices.append(chain.forward(angles))
    if len(matrices) < 2:
        return 0.0
    return max(
        _rotation_angle(matrices[i], matrices[j])
        for i in range(len(matrices))
        for j in range(i + 1, len(matrices))
    )
