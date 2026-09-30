"""自动移调 / 清理 / 主旋律提取 / 按键谱导出 的测试。"""
import pretty_midi

from piano_tool.midi_mapper import (
    clean_notes,
    estimate_transposition,
    export_key_notation,
    map_midi_to_events,
    pick_melody,
)
from piano_tool.models import NoteEvent


def _note(pitch, start, end, velocity=100):
    return pretty_midi.Note(velocity=velocity, pitch=pitch, start=start, end=end)


def _write_midi(notes, tmp_path, name="t.mid"):
    pm = pretty_midi.PrettyMIDI()
    inst = pretty_midi.Instrument(program=0, name="t")
    inst.notes.extend(notes)
    pm.instruments.append(inst)
    p = tmp_path / name
    pm.write(str(p))
    return p


def test_estimate_transposition_finds_white_friendly_key():
    """整首都在 C# 上 → 应移调 -1（或 +11 等价），使白键命中 100%。"""
    notes = [_note(61, 0.0, 1.0), _note(61, 1.0, 2.0), _note(73, 2.0, 3.0)]
    shift, rate = estimate_transposition(notes)
    assert rate > 0.99
    assert shift in (-1, 11)


def test_estimate_transposition_c_major_keeps_zero():
    """C 大调无需移调。"""
    notes = [_note(60, 0, 1), _note(62, 1, 2), _note(64, 2, 3), _note(67, 3, 4)]
    shift, rate = estimate_transposition(notes)
    assert shift == 0
    assert rate > 0.99


def test_clean_notes_drops_short_and_duplicates():
    notes = [
        _note(60, 0.00, 0.01),   # 过短 → 丢弃
        _note(60, 0.10, 0.60),   # 保留
        _note(60, 0.12, 0.60),   # 同音高且太近 → 丢弃
        _note(62, 0.50, 1.00),   # 保留
    ]
    kept = clean_notes(notes, min_duration=0.06, dedupe_gap=0.06)
    assert [n.pitch for n in kept] == [60, 62]


def test_pick_melody_keeps_loudest_per_onset():
    notes = [
        _note(60, 0.00, 0.50, velocity=40),   # 和声，弱
        _note(67, 0.00, 0.50, velocity=120),  # 主旋律，强
        _note(64, 0.01, 0.50, velocity=60),
        _note(62, 1.00, 1.50, velocity=50),   # 下一拍，单独
    ]
    picked = pick_melody(notes, window=0.05)
    assert [n.pitch for n in picked] == [67, 62]


def test_auto_transpose_reduces_altered_notes(tmp_path):
    """同一段旋律，开启自动移调后被改动的音应明显更少。"""
    # F# 大调色彩（大量黑键）
    notes = [_note(66, i * 0.5, i * 0.5 + 0.45) for i in range(8)]
    notes += [_note(61, i * 0.5, i * 0.5 + 0.45) for i in range(4)]
    path = _write_midi(notes, tmp_path, "fsharp.mid")

    _, off = map_midi_to_events(path, auto_transpose=False)
    _, on = map_midi_to_events(path, auto_transpose=True)
    assert on["black_replaced"] < off["black_replaced"]
    assert on["white_hit_rate"] > off["white_hit_rate"]


def test_mono_option_reduces_note_count(tmp_path):
    """每个 onset 放 3 个音，开启 mono 后应只剩 1/3。"""
    notes = []
    for i in range(6):
        for p in (60, 64, 67):
            notes.append(_note(p, i * 0.5, i * 0.5 + 0.4, velocity=60 + p % 10))
    path = _write_midi(notes, tmp_path, "chords.mid")
    _, poly = map_midi_to_events(path, mono=False)
    _, mono = map_midi_to_events(path, mono=True)
    assert mono["total"] == 6
    assert poly["total"] == 18


def test_same_key_duplicates_are_removed(tmp_path):
    """不同音高被吸附到同一白键、且几乎同时 → 只保留一次点击。"""
    notes = [_note(61, 0.0, 0.5), _note(60, 0.0, 0.5)]  # C# 与 C 会吸到同一个键附近
    path = _write_midi(notes, tmp_path, "dup.mid")
    events, stats = map_midi_to_events(path, auto_transpose=False)
    # 60 → 60；61 → 吸附到 60 或 62，若同为 60 则应被去重
    keys_at_start = [e.key for e in events if e.time < 0.05]
    assert len(keys_at_start) == len(set(keys_at_start))
    assert stats["dup_removed"] >= 0


def test_export_key_notation_format():
    """单音写字母，和弦写括号；大间隔换行。"""
    events = [
        NoteEvent(time=0.0, note=60, key="Z", action="press", duration=0.2),
        NoteEvent(time=0.30, note=62, key="X", action="press", duration=0.2),
        NoteEvent(time=1.00, note=64, key="C", action="press", duration=0.2),
        NoteEvent(time=1.00, note=67, key="B", action="press", duration=0.2),
    ]
    text = export_key_notation(events, phrase_gap=0.6, note_gap=0.18)
    lines = text.split("\n\n")
    assert lines[0].replace(" ", "") == "ZX"      # 0.30s 间隔 → 空格分句
    assert "(CB)" in lines[1]                      # 同时发声 → 和弦括号（按音高从低到高）
