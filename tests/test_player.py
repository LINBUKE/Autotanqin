"""⑤ player 测试：mock pyautogui / rich，验证 dry-run 与真实点击。"""
import json
import sys
from types import SimpleNamespace

import pytest

from piano_tool import player as player_mod
from piano_tool.locator import build_calibration
from piano_tool.models import NoteEvent


@pytest.fixture
def calibration():
    corners = [[100, 100], [800, 100], [800, 400], [100, 400]]
    return build_calibration(corners, image_size=[900, 500])


@pytest.fixture
def events():
    return [
        NoteEvent(time=0.0, note=60, key="Z", action="press", duration=0.4),
        NoteEvent(time=0.5, note=62, key="X", action="press", duration=0.4),
        NoteEvent(time=1.0, note=64, key="C", action="press", duration=0.4),
    ]


def _mock_gui(mocker):
    clicks = []
    fake_gui = SimpleNamespace(
        FAILSAFE=False,
        PAUSE=0,
        MINIMUM_DURATION=0,
        MINIMUM_SLEEP=0,
        click=lambda x, y, **kw: clicks.append((x, y)),
        FailSafeException=Exception,
    )
    mocker.patch.dict(sys.modules, {"pyautogui": fake_gui})

    fake_rich = SimpleNamespace(
        progress=SimpleNamespace(Progress=lambda: _FakeProgress())
    )
    mocker.patch.dict(sys.modules, {"rich": fake_rich, "rich.progress": fake_rich.progress})
    return clicks


class _FakeProgress:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def add_task(self, *a, **k):
        return 0

    def advance(self, *a, **k):
        pass


def test_dry_run_no_click(calibration, events, mocker):
    clicks = _mock_gui(mocker)
    devs = player_mod.play(events, calibration, dry_run=True, delay_ms=0)
    assert len(devs) == 3
    assert clicks == []  # dry-run 不应点击


def test_real_run_clicks_coordinates(calibration, events, mocker):
    clicks = _mock_gui(mocker)
    devs = player_mod.play(events, calibration, dry_run=False, delay_ms=0)
    assert len(clicks) == 3
    # 第一次点击应是 Z 键坐标（C4 网格位置）
    z_coord = next(k for k in calibration.keys if k.key == "Z")
    assert clicks[0][0] == pytest.approx(z_coord.x, abs=1e-3)
    assert clicks[0][1] == pytest.approx(z_coord.y, abs=1e-3)


def test_stop_check_stops_early(calibration, events, mocker):
    """stop_check 返回 True 时应立即停止，不再继续点击。"""
    clicks = _mock_gui(mocker)
    calls = {"n": 0}

    def stop_after_first():
        calls["n"] += 1
        return calls["n"] > 1  # 第 2 个音就停

    devs = player_mod.play(events, calibration, dry_run=False, delay_ms=0,
                           stop_check=stop_after_first)
    assert len(clicks) == 1
    assert len(devs) == 1


def test_per_key_latency_lead_does_not_change_count(calibration, events, mocker):
    """传入逐键提前量时仍应正常点击，且次数不变（只影响触发时刻）。"""
    clicks = _mock_gui(mocker)
    devs = player_mod.play(
        events, calibration, dry_run=False, delay_ms=0,
        latency_comp_ms=0.0, latency_lead={"Z": 0.1, "X": 0.2, "C": 0.15},
    )
    assert len(clicks) == 3
    assert len(devs) == 3


def test_load_roundtrip(tmp_path, calibration, events):
    # 写出 events.json 再读回
    ep = tmp_path / "events.json"
    ep.write_text(json.dumps([e.model_dump() for e in events], ensure_ascii=False), encoding="utf-8")
    cp = tmp_path / "cal.json"
    cp.write_text(calibration.model_dump_json(), encoding="utf-8")

    evs = player_mod.load_events(ep)
    cal = player_mod.load_calibration(cp)
    assert len(evs) == 3
    assert len(cal.keys) == 21


def test_duration_survives_write_and_load(tmp_path, events):
    """build_events_json/write_events 必须保留 duration，否则按住时值拿不到长短。"""
    from piano_tool.midi_mapper import write_events

    ep = tmp_path / "events.json"
    write_events(events, ep)
    loaded = player_mod.load_events(ep)
    assert len(loaded) == 3
    assert loaded[0].duration == pytest.approx(0.4)
    assert loaded[2].duration == pytest.approx(0.4)


def test_build_hold_schedule_preserves_durations(calibration):
    """按住时值时间线：每个音符拆成 down/up，时长比例正确。"""
    key_to_coord = {k.key: (k.x, k.y) for k in calibration.keys}
    evs = [
        NoteEvent(time=0.0, note=60, key="Z", duration=0.4),
        NoteEvent(time=0.5, note=62, key="X", duration=0.1),
    ]
    sched = player_mod.build_hold_schedule(
        evs, key_to_coord, base_lead=0.0, latency_lead=None,
        speed=1.0, start_time=0.0, hold_min=0.03, delay_ms=0, hold=True,
    )
    times = [s[0] for s in sched]
    kinds = [s[1] for s in sched]
    # Z: down@0, up@0.4 ; X: down@0.5, up@0.6
    assert times == pytest.approx([0.0, 0.4, 0.5, 0.6])
    assert kinds == ["down", "up", "down", "up"]
    # 时长 0.4 的音比 0.1 的音抬得更晚
    assert sched[1][0] == pytest.approx(0.4)
    assert sched[3][0] == pytest.approx(0.6)


def test_hold_mode_presses_and_releases(mocker, calibration):
    """hold=True 应每个音符 mouseDown 一次、mouseUp 一次（含无时值的音符）。"""
    down, up = [], []
    fake_gui = SimpleNamespace(
        FAILSAFE=False, PAUSE=0, MINIMUM_DURATION=0, MINIMUM_SLEEP=0,
        mouseDown=lambda x, y, **kw: down.append((x, y)),
        mouseUp=lambda x, y, **kw: up.append((x, y)),
        FailSafeException=Exception,
    )
    mocker.patch.dict(sys.modules, {"pyautogui": fake_gui})
    fake_rich = SimpleNamespace(
        progress=SimpleNamespace(Progress=lambda: _FakeProgress())
    )
    mocker.patch.dict(sys.modules, {"rich": fake_rich, "rich.progress": fake_rich.progress})
    # 跳过真实 sleep，避免 0.6s 等待
    mocker.patch("piano_tool.player.time.sleep", lambda *a, **k: None)

    evs = [
        NoteEvent(time=0.0, note=60, key="Z", duration=0.4),
        NoteEvent(time=0.5, note=62, key="X", duration=0.4),
        NoteEvent(time=1.0, note=64, key="C", duration=None),  # 无时值：退化为点击
    ]
    devs = player_mod.play(evs, calibration, dry_run=False, delay_ms=0, hold=True)
    # 每个音符都有一次按下与抬起
    assert len(down) == 3
    assert len(up) == 3
    # 顺序：down Z, up Z, down X, up X, down C, up C（按时间线）
    z = next(k for k in calibration.keys if k.key == "Z")
    c = next(k for k in calibration.keys if k.key == "C")
    assert down[0] == (pytest.approx(z.x), pytest.approx(z.y))
    assert down[2] == (pytest.approx(c.x), pytest.approx(c.y))
    assert len(devs) == 3  # 每个 down 记一次偏差


def test_hold_mode_serializes_overlapping_short_notes(mocker, calibration):
    """密集短音相互重叠时，必须先用 mouseUp 抬起上一个键再按新键。

    否则单鼠标按钮冲突：新键的 mouseDown 被当成“已按下”而无声，
    表现为短音漏音/被截断 → 听感卡顿（用户反馈的“米粒一样短音卡卡”）。
    """
    calls = []

    def _down(x, y, **kw):
        calls.append(("down", x, y))

    def _up(x, y, **kw):
        calls.append(("up", x, y))

    fake_gui = SimpleNamespace(
        FAILSAFE=False, PAUSE=0, MINIMUM_DURATION=0, MINIMUM_SLEEP=0,
        mouseDown=_down, mouseUp=_up, FailSafeException=Exception,
    )
    mocker.patch.dict(sys.modules, {"pyautogui": fake_gui})
    fake_rich = SimpleNamespace(
        progress=SimpleNamespace(Progress=lambda: _FakeProgress())
    )
    mocker.patch.dict(sys.modules, {"rich": fake_rich, "rich.progress": fake_rich.progress})
    mocker.patch("piano_tool.player.time.sleep", lambda *a, **k: None)

    # 三个极短且相互重叠的音（间隔 20ms < hold_min 30ms）
    evs = [
        NoteEvent(time=0.0, note=60, key="Z", duration=0.02),
        NoteEvent(time=0.02, note=62, key="X", duration=0.02),
        NoteEvent(time=0.04, note=64, key="C", duration=0.02),
    ]
    player_mod.play(evs, calibration, dry_run=False, delay_ms=0,
                    hold=True, hold_min_ms=30)

    # 回放调用序列：任何“按下不同键”之前，上一个键必须先被抬起
    # （单指弹奏，任意时刻至多一个键处于按下态）
    active = set()
    for action, x, y in calls:
        key = next((k.key for k in calibration.keys
                    if abs(k.x - x) < 1e-3 and abs(k.y - y) < 1e-3), None)
        if action == "down":
            assert not (active and key not in active), (
                f"在键 {sorted(active)} 仍按住时又按下了 {key}：短音会漏音/卡顿"
            )
            active.add(key)
        else:
            active.discard(key)
