"""伴奏轨：弹琴谱的同时放**分离出来的纯伴奏**（`no_vocals.wav`）。

**为什么是纯伴奏而不是原曲、也不是 song.mid**

用户要的是「一边弹一边听原来的伴奏，好对比出琴谱哪里不对」：
- 原曲带人声，人声会盖住琴声，对比反而听不清；
- `song.mid` 是 ① 转写出来的 MIDI（还没经过 ② 映射），用系统合成器放出来音色
  塑料、还只是转写的近似，听着不像"原曲"；
- `no_vocals.wav` 是 demucs 分离出的伴奏轨 —— 就是原曲去掉人声，最接近用户想要的"对比"。

**为什么用 sounddevice 而不是原来的 MCI**

MCI 的 sequencer 设备**不能调音量**（`setaudio volume` 是 waveaudio 的能力），
而用户明确要求「可以调整伴奏音量」。用 `sounddevice.OutputStream` 播 wav：
每块数据乘一个增益即可实时改音量，顺便还能用「改采样率」的方式实现倍速播放。

**几个坑（都踩过）**

- 伴奏文件只有 ① 页勾了「先人声分离」才有。没有时不要报错，返回 False + 提示，
  主播放照常 —— 伴奏是锦上添花。
- 倍速播放靠把流的采样率改成 `sr × speed`：节奏对得上，但**音高会跟着升高**
  （像磁带快放）。只当节奏参考用，这一点要在日志里说清楚。
- `sd.OutputStream` 在部分设备上不接受奇怪的采样率（如 44100×1.5=66150），
  开流失败就退回原速并给 warning，别让伴奏把演奏带崩。
- 暂停时不关流、只输出静音并停住读取位置：关流再开会有几十毫秒爆音/延迟。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

#: 伴奏默认音量（相对原曲）。比琴声轻一点，避免盖住按键声。
DEFAULT_VOLUME = 0.6


def clamp_volume(v) -> float:
    """音量统一成 0.0~1.5 的增益（允许略微超过 1 用于补偿安静的伴奏）。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return DEFAULT_VOLUME
    if x != x:  # NaN
        return DEFAULT_VOLUME
    return max(0.0, min(1.5, x))


#: 伴奏来源：分离出的纯伴奏 / ① 页那首原曲。
SOURCE_STEM = "stem"
SOURCE_ORIGINAL = "original"

#: 下拉框展示文案（改文案不会让旧预设失效 —— `normalize_source` 按关键词认）。
SOURCE_LABELS = {
    SOURCE_STEM: "纯伴奏（无人声，分离自原曲）",
    SOURCE_ORIGINAL: "原曲（含人声）",
}


def normalize_source(value) -> str:
    """把下拉框文案 / 预设里的值统一成 `stem` 或 `original`。

    认不出来的（旧预设、空值、乱码）一律按 `stem` —— 那是更"干净"的默认，
    用人声原曲盖住琴声反而影响判断。
    """
    s = str(value or "").strip()
    if s in (SOURCE_STEM, SOURCE_ORIGINAL):
        return s
    low = s.lower()
    # **顺序很重要**：纯伴奏的文案是「纯伴奏（无人声，分离自原曲）」，里面也有「原曲」
    # 两个字；先认「原曲」会把它误判成原曲（人声盖住琴声，用户就听不出问题了）。
    if "纯伴奏" in s or "no_vocals" in low or low in ("stem", "accompaniment", "inst"):
        return SOURCE_STEM
    if "原曲" in s or low in ("original", "full", "mix", "song", "audio"):
        return SOURCE_ORIGINAL
    return SOURCE_STEM


def find_backing(audio_path=None, source: str = SOURCE_STEM,
                 separated_dir: Optional[Path] = None) -> Tuple[Optional[Path], str]:
    """按用户选的方式拿伴奏文件，返回 `(路径, 说明)`。

    - `source=stem` → 走 `find_backing_wav()`（demucs 分离出的 `no_vocals.wav`）；
    - `source=original` → 直接用 ① 页选的那首歌的文件本身（mp3/wav/flac 都行，
      soundfile 0.14 起能解 MP3）。

    找不到时路径为 None，说明直接给用户看（要能指导下一步动作，不能只说"失败"）。
    """
    if normalize_source(source) == SOURCE_ORIGINAL:
        p = Path(str(audio_path).strip()) if audio_path else None
        if p is not None and p.is_file():
            return p, f"原曲：{p.name}"
        return None, ("没找到原曲音频 —— 请先在 ① 页选好歌曲文件（MP3/WAV）；"
                      "只想用分离出的伴奏的话，把「伴奏来源」切回「纯伴奏」")
    return find_backing_wav(audio_path, separated_dir)


def find_backing_wav(audio_path=None,
                     separated_dir: Optional[Path] = None) -> Tuple[Optional[Path], str]:
    """找 `no_vocals.wav`。

    返回 `(路径, 说明)`；找不到时路径为 None、说明写清原因（直接给用户看）。

    查找顺序：
    1. 有 `audio_path`（① 页选的那首歌）→ 精确匹配 `<separated>/<模型>/<歌名>/no_vocals.wav`；
    2. 没给或没匹配上 → 在 `<separated>/*/*/no_vocals.wav` 里取**最近修改**的那个，
       并在说明里带上歌名（免得悄悄放错歌）。
    """
    if separated_dir is None:
        from .config import get_config

        separated_dir = get_config().output_dir / "separated"

    root = Path(separated_dir)
    if not root.is_dir():
        return None, "没有找到分离出来的伴奏（data/output/separated 还不存在）"

    stem = ""
    if audio_path:
        try:
            stem = Path(str(audio_path)).stem
        except Exception:  # noqa: BLE001
            stem = ""

    if stem:
        hits = [p for p in root.glob(f"*/{stem}/no_vocals.wav") if p.is_file()]
        if hits:
            hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return hits[0], f"伴奏：{stem}/no_vocals.wav"

    all_no_vocals = [p for p in root.glob("*/*/no_vocals.wav") if p.is_file()]
    if not all_no_vocals:
        return None, ("没找到分离出的纯伴奏。请先在 ① 页勾选「先人声分离再转写」"
                      "并转换一次，生成 no_vocals.wav")
    all_no_vocals.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    best = all_no_vocals[0]
    return best, f"伴奏：{best.parent.name}/no_vocals.wav（最近分离的那首）"


class BackingTrack:
    """把一段 wav 当伴奏播放，可调音量、可指定起点、可倍速。

    典型用法（播放线程里）::

        bg = BackingTrack(path, volume=0.6, speed=speed)
        if bg.start(from_sec=start_at):
            ...  # 主播放（这里阻塞）
        bg.stop()          # finally 里一定调

    `volume` 播放中随时可改（主线程直接赋值即可，音频回调每块都会读）。
    """

    def __init__(self, path, volume: float = DEFAULT_VOLUME, speed: float = 1.0):
        self.path = Path(path) if path else None
        self.volume = clamp_volume(volume)
        self.speed = max(0.25, min(4.0, float(speed or 1.0)))
        self.warning = ""
        self._data = None          # (frames, channels) float32
        self._sr = 0
        self._pos = 0              # 已读到的帧号（源文件坐标）
        self._paused = False
        self._stream = None
        self._finished = False

    # -- 生命周期 ---------------------------------------------------------
    def start(self, from_sec: float = 0.0) -> bool:
        """从 `from_sec` 秒处开始放。成功返回 True，失败只给 `warning`，不抛。"""
        self.warning = ""
        self._finished = False
        if self.path is None or not self.path.is_file():
            self.warning = f"找不到伴奏文件：{self.path.name if self.path else '(未指定)'}"
            return False

        try:
            import soundfile as sf

            data, sr = sf.read(str(self.path), dtype="float32", always_2d=True)
        except Exception as exc:  # noqa: BLE001
            self.warning = f"伴奏读取失败：{exc}"
            return False
        if sr <= 0 or data.size == 0:
            self.warning = "伴奏文件是空的"
            return False

        self._data, self._sr = data, int(sr)
        self._pos = max(0, min(len(data), int(round(float(from_sec) * sr))))
        self._paused = False

        ok = self._open_stream(int(round(sr * self.speed)))
        if not ok and abs(self.speed - 1.0) > 1e-6:
            # 设备不认变速率 → 退回原速。节奏会越弹越偏，至少用户知道原因。
            if self._open_stream(sr):
                self.warning = (f"音频设备不支持 {int(round(sr * self.speed))}Hz 的变速率，"
                                f"伴奏已按原速播放（{self.speed:g}× 时会越来越不同步）")
                return True
        return ok

    def _open_stream(self, out_sr: int) -> bool:
        try:
            import sounddevice as sd

            self._stream = sd.OutputStream(
                samplerate=int(out_sr),
                channels=int(self._data.shape[1]),
                dtype="float32",
                callback=self._callback,
            )
            self._stream.start()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("伴奏音频流打不开（%s Hz）：%s", out_sr, exc)
            self._stream = None
            self.warning = f"伴奏放不出来（音频设备打不开 {out_sr}Hz）：{exc}"
            return False

    def _callback(self, outdata, frames, _time_info, status) -> None:
        """音频线程：从内存缓冲切片、乘音量写出去。"""
        if self._paused or self._data is None:
            outdata.fill(0)
            return
        pos = self._pos
        chunk = self._data[pos:pos + frames]
        n = len(chunk)
        if n:
            outdata[:n] = chunk
            outdata[:n] *= self.volume
        if n < frames:
            outdata[n:] = 0
            self._pos = len(self._data)
            self._finished = True
            import sounddevice as sd

            raise sd.CallbackStop
        self._pos = pos + frames

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def stop(self) -> None:
        """停止并释放设备。可重复调用。"""
        st, self._stream = self._stream, None
        if st is not None:
            try:
                st.stop()
                st.close()
            except Exception:  # noqa: BLE001  设备已经没了也无所谓
                pass
        self._data = None
        self._paused = False

    # -- 状态 -------------------------------------------------------------
    @property
    def playing(self) -> bool:
        return self._stream is not None and not self._finished

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def position(self) -> float:
        """当前播放到源文件的第几秒（诊断用）。"""
        if not self._sr:
            return 0.0
        return self._pos / float(self._sr)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.stop()
        return False
