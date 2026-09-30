"""「清理音符保持旋律」单测：清掉米粒短音、同音近重复，可选 skyline 抽旋律。

模拟 basic_pitch 把背景音/噪声误识成极短音符的场景，验证 clean_events_keep_melody
只删「明确带时值且小于阈值」的音，保留用户手动编辑（duration=None）的音。
"""
from piano_tool.midi_mapper import clean_events_keep_melody
from piano_tool.models import NoteEvent


def _ev(time, note, duration, key="Z"):
    return NoteEvent(time=time, note=note, key=key, action="press", duration=duration)


def _sample_events():
    """构造一份含米粒短音、同音近重复、手动编辑(None 时值)、同时刻双音的谱子。"""
    return [
        _ev(0.0, 60, 0.50),   # A 正常
        _ev(0.1, 62, 0.02),   # B 米粒短音(<60ms)
        _ev(0.3, 64, 0.40),   # C 正常
        _ev(0.32, 64, 0.40),  # D 与 C 同音且 <60ms 紧挨 → 重复毛刺
        _ev(0.5, 67, None),   # E 手动编辑(未设按住时长) → 必须保留
        _ev(1.0, 60, 0.30),   # F 同时刻低音
        _ev(1.0, 72, 0.30),   # G 同时刻高音
    ]


def test_drops_rice_grain_and_dupes_keeps_manual():
    events, stats = clean_events_keep_melody(_sample_events(), min_duration=0.06)
    notes = [e.note for e in events]
    assert stats["short_dropped"] == 1          # B 被清
    assert stats["dup_dropped"] == 1            # D 被清
    assert 67 in notes                          # E(None 时值) 保留
    assert 62 not in notes                      # 米粒音已消失
    assert 64 in notes and notes.count(64) == 1  # 同音只留一个
    assert stats["out"] == 5                     # A C E F G


def test_melody_extraction_keeps_skyline():
    events, stats = clean_events_keep_melody(
        _sample_events(), min_duration=0.06, melody=True, melody_mode="high"
    )
    notes = [e.note for e in events]
    assert 72 in notes
    # 同时刻 60/72 只留最高音 72：t≈1.0 处只有一个音且是 72（F 被 skyline 抽掉）
    simul = [e for e in events if abs(e.time - 1.0) < 1e-6]
    assert len(simul) == 1 and simul[0].note == 72
    # note 60 仍出现是来自 t=0.0 的 A（合法主旋律音），并非被重复保留
    assert notes.count(60) == 1
    assert stats["melody_dropped"] == 1
    assert stats["melody"] is True


def test_preserves_time_key_action_roundtrip():
    events, _ = clean_events_keep_melody(_sample_events())
    for e in events:
        assert e.action == "press"
        assert isinstance(e.time, float)
        assert e.key                       # key 未丢失
    # 时值按清理后实际时值回写（正常音 duration 不变）
    a = next(e for e in events if e.note == 60)
    assert abs(a.duration - 0.50) < 1e-6
