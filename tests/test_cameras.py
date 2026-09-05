from soarm_console.cameras import next_send_time


def deliver(arrivals: list[float], fps: float) -> list[float]:
    """도착 시각을 캡처 루프와 같은 순서로 흘려 보내고, 실제로 나간 시각만 돌려준다."""
    interval = 1 / fps if fps > 0 else 0
    next_send = 0.0
    sent: list[float] = []
    for now in arrivals:
        scheduled = next_send_time(now, next_send, interval)
        if scheduled is None:
            continue
        next_send = scheduled
        sent.append(now)
    return sent


def arrivals_at(
    fps: float, seconds: float, start: float = 100.0, jitter: float = 0.0
) -> list[float]:
    """장치가 주는 프레임의 도착 시각. 지터는 한 프레임 걸러 앞뒤로 흔든다."""
    step = 1 / fps
    return [
        start + index * step + (jitter if index % 2 else -jitter)
        for index in range(int(seconds * fps))
    ]


def test_a_device_slightly_slower_than_the_target_loses_no_frames():
    # 장치가 26.8fps를 주는데 30fps를 요청한 자리다. 예정 시각을 도착 시각으로 다시
    # 맞추던 때에는 지터로 조금 일찍 온 프레임이 문턱(1/30)에 걸려 버려지고 다음 프레임을
    # 한 주기 더 기다려, 26.8장 가운데 17.5장만 나갔다. 한 장도 버리지 않아야 한다.
    arrivals = arrivals_at(26.8, seconds=5, jitter=0.004)

    assert deliver(arrivals, fps=30) == arrivals


def test_a_target_far_below_the_device_passes_exactly_the_target_rate():
    # 절약(2fps). 장치는 26.8fps를 주지만 초당 2장만 나가야 한다.
    sent = deliver(arrivals_at(26.8, seconds=10), fps=2)

    # 10초에 20장. 한 장이 더 있는 것은 첫 프레임이다 — 예정 시각이 0에서 출발하므로
    # 스트림을 여는 순간 한 장이 덤으로 나가고, 그 다음부터 눈금에 올라탄다.
    assert len(sent) == 21
    gaps = [later - earlier for earlier, later in zip(sent[1:], sent[2:])]
    # 예정 시각은 정확히 0.5초 눈금 위에 있지만 실제로 나가는 것은 그 눈금을 지난 첫
    # 도착 프레임이므로, 간격은 도착 주기 하나만큼만 0.5초에서 흔들린다.
    assert all(abs(gap - 0.5) <= 1 / 26.8 for gap in gaps)


def test_a_long_gap_does_not_release_a_burst_when_the_stream_comes_back():
    # 스트림이 10초 끊겼다 붙는다.
    before = arrivals_at(26.8, seconds=1, start=100.0)
    after = arrivals_at(26.8, seconds=3, start=111.0)

    resumed = [when for when in deliver(before + after, fps=2) if when >= 111.0]

    # 재개 직후 1초에 3장 — 눈금 위의 두 장과, 끊긴 자리를 지나며 예정 시각이 현재로
    # 당겨질 때 나가는 한 장이다. `max(now, ...)` 없이 밀린 예정 시각을 그대로 더했다면
    # 같은 자리에서 27장이 몰려 나갔다.
    assert len([when for when in resumed if when < 112.0]) == 3
    assert len(resumed) == 7


def test_an_unset_frame_rate_passes_every_frame():
    arrivals = arrivals_at(26.8, seconds=1)

    assert deliver(arrivals, fps=0) == arrivals
