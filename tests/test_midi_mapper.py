"""② midi_mapper 测试：覆盖 C 大调音阶、含黑键旋律、超范围、和弦。"""
import pretty_midi
import pytest

from piano_tool.midi_mapper import map_midi_to_events
from piano_tool.config import WHITE_MIDIS, KEY_BY_MIDI


def _make_midi(notes):
    """notes: list of (pitch, start, end)"""
    pm = pretty_midi.PrettyMIDI()
    inst = pretty_midi.Instrument(program=0, name="t")
    for p, s, e in notes:
        inst.notes.append(pretty_midi.Note(velocity=100, pitch=p, start=s, end=e))
    pm.instruments.append(inst)
    return pm


def _write(pm, tmp_path, name="x.mid"):
    path = tmp_path / name
    pm.write(str(path))
    return path


def test_c_major_scale_all_white(tmp_path):
    # C4 D4 E4 F4 G4 A4 B4
    notes = [(60, 0.0, 0.5), (62, 0.5, 1.0), (64, 1.0, 1.5),
             (65, 1.5, 2.0), (67, 2.0, 2.5), (69, 2.5, 3.0), (71, 3.0, 3.5)]
    path = _write(_make_midi(notes), tmp_path, "scale.mid")
    events, stats = map_midi_to_events(path)
    assert stats["total"] == 7
    assert stats["black_replaced"] == 0
    assert stats["octave_folded"] == 0
    assert stats["white_kept"] == 7
    for e in events:
        assert e.note in WHITE_MIDIS
        assert e.key == KEY_BY_MIDI[e.note]


def test_black_key_melody_replaced(tmp_path):
    # 含 C#(61)/D#(63)/F#(66)
    # 这里关掉自动移调，测的是「纯吸附」行为；开启移调后改动数会更少（见下个用例）
    notes = [(60, 0.0, 0.5), (61, 0.5, 1.0), (62, 1.0, 1.5),
             (63, 1.5, 2.0), (64, 2.0, 2.5), (66, 2.5, 3.0)]
    path = _write(_make_midi(notes), tmp_path, "black.mid")
    events, stats = map_midi_to_events(path, auto_transpose=False)
    assert stats["black_replaced"] == 3
    for e in events:
        assert e.note in WHITE_MIDIS
    # C#(61) 应映射到最近的 D(62) 或 C(60)
    mapped = {orig: e.note for orig, e in zip([60, 61, 62, 63, 64, 66], events)}
    assert mapped[61] in (60, 62)


def test_auto_transpose_reduces_black_replacement(tmp_path):
    """同样的旋律，开启自动移调后需要改动音高的音符更少。"""
    notes = [(60, 0.0, 0.5), (61, 0.5, 1.0), (62, 1.0, 1.5),
             (63, 1.5, 2.0), (64, 2.0, 2.5), (66, 2.5, 3.0)]
    path = _write(_make_midi(notes), tmp_path, "black.mid")
    _, off = map_midi_to_events(path, auto_transpose=False)
    _, on = map_midi_to_events(path, auto_transpose=True)
    assert off["black_replaced"] == 3
    assert on["black_replaced"] < off["black_replaced"]
    assert on["white_hit_rate"] > off["white_hit_rate"]


def test_out_of_range_octave_fold(tmp_path):
    # C2(36) 远低于 C4(60)，应向上折叠到 C4(60)
    notes = [(36, 0.0, 0.5), (95, 1.0, 1.5)]  # B6 在边界内
    path = _write(_make_midi(notes), tmp_path, "range.mid")
    events, stats = map_midi_to_events(path)
    assert stats["octave_folded"] == 1
    assert events[0].note == 60  # 折叠到 C4
    assert events[1].note == 95  # B6 保持


def test_chord_preserved(tmp_path):
    # 同一时刻三音和弦
    notes = [(60, 0.0, 1.0), (64, 0.0, 1.0), (67, 0.0, 1.0)]
    path = _write(_make_midi(notes), tmp_path, "chord.mid")
    events, stats = map_midi_to_events(path)
    assert stats["total"] == 3
    assert sorted(e.note for e in events) == [60, 64, 67]


def test_events_sorted_by_time(tmp_path):
    notes = [(67, 2.0, 2.5), (60, 0.0, 0.5), (64, 1.0, 1.5)]
    path = _write(_make_midi(notes), tmp_path, "unsorted.mid")
    events, _ = map_midi_to_events(path)
    times = [e.time for e in events]
    assert times == sorted(times)
