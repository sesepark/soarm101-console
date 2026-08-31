"""가상 리더 — 물리 리더 팔 없이 팔로워를 조작하는 경로.

`lerobot-teleoperate`를 서브프로세스로 띄우던 기존 텔레옵은 리더 팔의 관절값을 팔로워에
그대로 흘려보낸다. 그 구조에는 바깥에서 목표를 넣을 자리가 없다. 여기서는 서버가 직접
팔로워 serial을 쥐고 30Hz 루프를 돌면서, 검증을 통과한 목표 관절값을 받아 쓴다.

세 조각으로 나뉜다.

- `owner.VirtualLeaderOwner` — 장치를 쥐고 도는 제어 루프. 장치 소유자는 언제나 하나다.
- `authority.AuthorityManager` — Follower motion 리스. 동시에 하나만 발급한다.
- `safety` — 명령 하나에 대한 검사 사다리와 관측이 거는 정지.

`teleoperator.SOArmVirtualLeader`는 이 경로를 LeRobot의 teleoperator로 감싼 것이다.
`lerobot-record`가 물리 리더 대신 이것을 쓰면 수집 화면을 새로 만들 필요가 없다.
"""

from .api import VirtualLeader, build_router
from .teleoperator import SOArmVirtualLeader, SOArmVirtualLeaderConfig

__all__ = [
    "SOArmVirtualLeader",
    "SOArmVirtualLeaderConfig",
    "VirtualLeader",
    "build_router",
]
