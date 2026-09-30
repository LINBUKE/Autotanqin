"""① audio_to_midi 测试：mock basic_pitch 与 pretty_midi。"""
import sys
import types
from types import SimpleNamespace

import piano_tool.audio_to_midi as mod


def test_audio_to_midi_counts_and_writes(tmp_path, mocker):
    fake_notes = [(0.0, 0.5, 60), (0.5, 1.0, 64), (1.0, 1.5, 67)]

    # basic_pitch 需要是真实模块对象，才能支持 `from basic_pitch.inference import predict`
    fake_inference = types.ModuleType("basic_pitch.inference")
    # predict 真实返回 (model_output, midi_data, note_events)，代码取第三个元素
    fake_inference.predict = lambda *a, **k: (None, None, fake_notes)
    fake_basic = types.ModuleType("basic_pitch")
    fake_basic.ICASSP_2022_MODEL_PATH = "fake_model"
    fake_basic.inference = fake_inference
    mocker.patch.dict(
        sys.modules,
        {"basic_pitch": fake_basic, "basic_pitch.inference": fake_inference},
    )

    captured = {}

    class FakeNote:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class FakeInstrument:
        def __init__(self, *a, **k):
            self.notes = []

    class FakeMidi:
        def __init__(self, *a, **k):
            self.instruments = []

        def write(self, p):
            captured["path"] = p

    fake_pm = SimpleNamespace(
        PrettyMIDI=FakeMidi,
        Instrument=FakeInstrument,
        Note=FakeNote,
    )
    mocker.patch.dict(sys.modules, {"pretty_midi": fake_pm})

    out = tmp_path / "out.mid"
    count, path = mod.audio_to_midi("song.mp3", out)

    assert count == 3
    assert path == out
    assert captured["path"] == str(out)
