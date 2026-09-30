"""① 音频转 MIDI（复音转写）

使用 basic_pitch 的 Python API 做音频复音转写，输出标准 MIDI 文件。
重型第三方依赖（basic_pitch / pretty_midi / scipy）采用惰性导入，
以便单元测试可以用 mock 替换而不必安装 tensorflow。

CLI 用法：
    python -m piano_tool.audio_to_midi input.mp3 -o output.mid
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .models import NoteEvent  # noqa: F401  (re-export，便于统一导入)

logger = logging.getLogger("piano_tool.audio_to_midi")


def audio_to_midi(
    audio_path,
    output_midi,
    model_path=None,
    onset_threshold: float = 0.5,
    frame_threshold: float = 0.3,
    minimum_note_length: int = 58,
    minimum_frequency: float = 0.0,
    maximum_frequency: float = 2000.0,
):
    """把音频转写为 MIDI。

    返回 (音符数量, 输出路径)。
    """
    # 惰性导入重型依赖
    from basic_pitch.inference import predict
    from basic_pitch import ICASSP_2022_MODEL_PATH
    import pretty_midi

    audio_path = str(audio_path)
    model = model_path or ICASSP_2022_MODEL_PATH

    # basic_pitch 用 None 表示"不限制"该频率边界；
    # <=0 的默认值在 librosa.hz_to_midi(0) 上会算出 -inf 导致溢出，故转成 None。
    min_freq = minimum_frequency if (minimum_frequency and minimum_frequency > 0) else None
    max_freq = maximum_frequency if (maximum_frequency and maximum_frequency > 0) else None

    logger.info("开始转写：%s", audio_path)
    # basic_pitch.predict 返回 (model_output, midi_data, note_events)
    # 第三个元素 note_events 才是 [(start, end, pitch, velocity, pitch_bend), ...]
    _, _, notes = predict(
        audio_path,
        model,
        onset_threshold=onset_threshold,
        frame_threshold=frame_threshold,
        minimum_note_length=minimum_note_length,
        minimum_frequency=min_freq,
        maximum_frequency=max_freq,
    )

    midi = pretty_midi.PrettyMIDI()
    instrument = pretty_midi.Instrument(program=0, name="Windsong Lyre")
    count = 0
    for onset, offset, pitch, *rest in notes:
        onset = float(onset)
        offset = float(offset)
        if offset <= onset:
            offset = onset + 0.1
        # basic_pitch 的第 4 个字段是响度(0~1)，写进 MIDI velocity，
        # 后续「主旋律提取」要靠它判断哪个音是主音（原来写死 100 会丢掉这个信息）。
        amp = rest[0] if rest else None
        try:
            velocity = int(max(1, min(127, round(float(amp) * 127)))) if amp is not None else 100
        except (TypeError, ValueError):
            velocity = 100
        instrument.notes.append(
            pretty_midi.Note(
                velocity=velocity,
                pitch=int(round(pitch)),
                start=onset,
                end=offset,
            )
        )
        count += 1
    midi.instruments.append(instrument)

    out = Path(output_midi)
    out.parent.mkdir(parents=True, exist_ok=True)
    midi.write(str(out))
    logger.info("转写完成：%d 个音符 -> %s", count, out)
    return count, out


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m piano_tool.audio_to_midi",
        description="音频 → MIDI 复音转写",
    )
    parser.add_argument("input", help="输入音频（MP3/WAV）")
    parser.add_argument("-o", "--output", default="data/output/song.mid", help="输出 MIDI 路径")
    parser.add_argument("--onset-threshold", type=float, default=0.5)
    parser.add_argument("--frame-threshold", type=float, default=0.3)
    parser.add_argument(
        "--minimum-frequency",
        type=float,
        default=0.0,
        help="只保留高于此频率的音符（Hz），默认 0 表示不限制下限",
    )
    parser.add_argument(
        "--maximum-frequency",
        type=float,
        default=2000.0,
        help="只保留低于此频率的音符（Hz）。风物之诗琴最高音 B6≈1975Hz，"
        "默认值 2000.0 已覆盖全部 21 个白键",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    count, out = audio_to_midi(
        args.input,
        args.output,
        onset_threshold=args.onset_threshold,
        frame_threshold=args.frame_threshold,
        minimum_frequency=args.minimum_frequency,
        maximum_frequency=args.maximum_frequency,
    )
    print(f"✓ 转写完成：{count} 个音符 -> {out}")


if __name__ == "__main__":
    main()
