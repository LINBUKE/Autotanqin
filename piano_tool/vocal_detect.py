"""人声匹配 · 自动识别音节

为什么做这个
------------
手动输入歌词只是为了知道「这句有几个字」，但用户真正要的是**一个音节一个音**；
而打字本身很麻烦。这个模块直接从人声轨里把音节切出来，不再需要歌词文本。

怎么做
------
1. **能量包络**：找出真正有人在唱的段落（安静处＝换气 / 间奏）。
2. **频谱通量**：人声连唱时字与字之间没有停顿，靠「能量突然上涨」找字头
   （辅音起音、元音切换），这就是常说的 onset detection。
3. **音节区间**：每个字头到下一个字头（或换气点）算一个音节。
4. **基频**：对每个音节取中段做自相关，估一下实际唱多高——用来在琴谱里
   「同一个音节对应好几个音」时挑最接近的那个，而不是随便挑。

对齐原则
--------
  · 只做「挑音 / 拆音 / 丢弃」，**不生成新音高**；
  · 一个音节最多留一个音（人声一次只能唱一个字）；
  · 一个长音跨过两个音节时，在音节边界把它拆开（同一个音高，时值各一半）；
  · 不在任何音节里的音（伴奏、噪声、转写毛刺）丢掉，并在统计里如实报数。

依赖只有 numpy + soundfile（项目已有），不引入 ASR 模型：唱歌的自动听写
（尤其带伴奏、带颤音）时间戳并不可靠，而这里本来就不需要文字。
"""
from __future__ import annotations

import bisect
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .models import NoteEvent

logger = logging.getLogger("piano_tool.vocal_detect")

__all__ = [
    "Syllable",
    "align_notes_to_syllables",
    "detect_syllables",
    "find_vocal_tracks",
]


@dataclass
class Syllable:
    """一个音节（一个字）在音频里的位置。"""

    start: float
    end: float
    midi: Optional[float] = None   # 实测基频（MIDI 浮点），估不出来就是 None

    @property
    def duration(self) -> float:
        return max(0.0, float(self.end) - float(self.start))


# ---------------------------------------------------------------- 音频读取

def _read_mono(audio_path) -> Tuple["object", int]:
    """读成单声道 float32；立体声取平均。"""
    import numpy as np
    import soundfile as sf

    data, sr = sf.read(str(audio_path), always_2d=True, dtype="float32")
    if data.size == 0:
        raise ValueError("音频是空的")
    y = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
    return np.ascontiguousarray(y, dtype=np.float32), int(sr)


def _smooth(x, k: int):
    import numpy as np

    if k <= 1:
        return x
    kernel = np.ones(k, dtype=np.float32) / float(k)
    return np.convolve(x, kernel, mode="same")


def _runs(mask) -> List[Tuple[int, int]]:
    """布尔序列里连续 True 的 [start, end) 下标。"""
    import numpy as np

    padded = np.concatenate(([False], mask.astype(bool), [False]))
    edges = np.flatnonzero(np.diff(padded.astype(np.int8)))
    return [(int(s), int(e)) for s, e in zip(edges[0::2], edges[1::2])]


def _merge_runs(mask, gap_frames: int, short_frames: int) -> List[Tuple[int, int]]:
    """把发声段按「允许的停顿」合并，并丢掉太短的碎片。

    不用形态学膨胀/腐蚀：那种做法会把每段的首尾各撑开半个核，
    音节起点会整体偏早，时间轴对齐全跟着偏。
    """
    merged: List[Tuple[int, int]] = []
    for s, e in _runs(mask):
        if merged and s - merged[-1][1] <= gap_frames:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return [(s, e) for s, e in merged if e - s >= short_frames]


# ---------------------------------------------------------------- 基频估计

def _frame_f0_midi(seg, sr: int) -> Optional[float]:
    """一帧的自相关基频（MIDI 浮点）。置信度太低返回 None。"""
    import numpy as np

    seg = np.asarray(seg, dtype=np.float32)
    if len(seg) < 64:
        return None
    seg = seg - float(seg.mean())
    if float(np.max(np.abs(seg))) < 1e-5:
        return None
    n = len(seg)
    spec = np.fft.rfft(seg, 2 * n)
    ac = np.fft.irfft(spec * np.conj(spec))[:n]
    if ac[0] <= 0:
        return None
    ac = ac / float(ac[0])
    min_lag = max(2, int(sr / 1100.0))          # 最高 1100Hz
    max_lag = min(n - 2, int(sr / 70.0))        # 最低 70Hz
    if max_lag <= min_lag + 2:
        return None
    seg_ac = ac[min_lag:max_lag]
    k = int(np.argmax(seg_ac))
    if float(seg_ac[k]) < 0.30:                 # 不像周期信号（气声/噪声）
        return None
    delta = 0.0
    if 0 < k < len(seg_ac) - 1:
        y0, y1, y2 = float(seg_ac[k - 1]), float(seg_ac[k]), float(seg_ac[k + 1])
        denom = y0 - 2.0 * y1 + y2
        if abs(denom) > 1e-9:
            delta = 0.5 * (y0 - y2) / denom
    lag = min_lag + k + delta
    if lag <= 0:
        return None
    hz = sr / lag
    if not (60.0 <= hz <= 1200.0):
        return None
    return 69.0 + 12.0 * math.log2(hz / 440.0)


def _syllable_midi(y, sr: int, start: float, end: float) -> Optional[float]:
    """一个音节中段的实测音高（多帧中位数）。"""
    import numpy as np

    dur = max(0.0, end - start)
    if dur <= 0.02:
        return None
    win = max(64, min(int(0.045 * sr), int(dur * sr)))
    a0 = int((start + 0.18 * dur) * sr)
    a1 = int((end - 0.18 * dur) * sr)
    if a1 - a0 < win:
        a0 = int((start + 0.5 * dur) * sr) - win // 2
        a1 = a0 + win
    if a0 < 0:
        a0, a1 = 0, win
    if a1 > len(y):
        return None
    # 在可用的区间里等距取最多 5 帧
    last = max(a0, a1 - win)
    points = np.linspace(a0, last, num=min(5, max(1, (last - a0) // max(1, win // 2) + 1)))
    vals = []
    for p in points:
        v = _frame_f0_midi(y[int(p):int(p) + win], sr)
        if v is not None:
            vals.append(v)
    if not vals:
        return None
    return float(np.median(vals))


# ---------------------------------------------------------------- 音节检测

def detect_syllables(
    audio_path,
    min_syllable_ms: float = 70.0,
    merge_gap_ms: float = 60.0,
    min_onset_gap_ms: float = 70.0,
    with_pitch: bool = True,
) -> List[Syllable]:
    """从人声轨检测音节，返回按时间排序的音节列表。

    参数
    ----
    min_syllable_ms: 短于这个时长的碎片不单独成字（气口/噪声）
    merge_gap_ms:   两个发声段间隔小于它就算连唱（中间没有换气）
    min_onset_gap_ms: 两个字头至少间隔这么久（防止一个起音被切成两个）
    with_pitch:     是否顺便估基频（用于挑音高）
    """
    import numpy as np

    y, sr = _read_mono(audio_path)
    if len(y) < int(sr * 0.2):
        raise ValueError("音频太短，听不出人声")

    frame = 1 << int(round(math.log2(max(256.0, sr * 0.04))))
    hop = max(64, int(sr * 0.01))
    n = 1 + max(0, (len(y) - frame) // hop)
    if n < 4:
        raise ValueError("音频太短，听不出人声")

    win = np.hanning(frame).astype(np.float32)
    flux = np.zeros(n, dtype=np.float32)
    rms = np.zeros(n, dtype=np.float32)
    offsets = np.arange(frame)
    prev = None
    chunk = 256
    for s in range(0, n, chunk):
        e = min(n, s + chunk)
        idx = np.arange(s, e)
        frames = y[idx[:, None] * hop + offsets[None, :]]
        rms[s:e] = np.sqrt(np.mean(frames * frames, axis=1))
        mag = np.log1p(np.abs(np.fft.rfft(frames * win, axis=1)) * 10.0)
        if prev is not None:
            combo = np.vstack((prev, mag))
            flux[s:e] = np.maximum(np.diff(combo, axis=0), 0.0).sum(axis=1)
        elif e - s > 1:
            flux[s + 1:e] = np.maximum(np.diff(mag, axis=0), 0.0).sum(axis=1)
        prev = mag[-1:]

    times = (np.arange(n, dtype=np.float64) * hop + frame / 2.0) / float(sr)
    frame_dt = hop / float(sr)

    # ---- 1. 哪里在发声：自适应门限（按整首歌的能量分布，不用固定 dB）----
    db = 20.0 * np.log10(rms + 1e-8)
    floor = float(np.percentile(db, 10))
    peak = float(np.percentile(db, 95))
    thr = floor + max(6.0, 0.28 * (peak - floor))
    mask = db > thr
    gap_frames = max(1, int(round(merge_gap_ms / 1000.0 / frame_dt)))
    short_frames = max(1, int(round(min_syllable_ms / 1000.0 / frame_dt)))
    regions = _merge_runs(mask, gap_frames, short_frames)   # 连唱合并、碎片剔除
    if not regions:
        return []

    # ---- 2. 字头：频谱通量峰 + 能量回升 ----
    # 只靠频谱通量会把「一个长音里的颤音/包络抖动」也当成新字，所以再要求
    # 起音处能量确实从低处涨上来（辅音、重新起音都符合；平唱不符合）。
    env = _smooth(flux, 3)
    local = _smooth(env, max(9, int(round(0.30 / frame_dt))))
    thr_curve = local + 0.06 * float(np.percentile(env, 98) + 1e-9)
    peaks = np.zeros(n, dtype=bool)
    peaks[1:-1] = (
        (env[1:-1] > env[:-2])
        & (env[1:-1] >= env[2:])
        & (env[1:-1] > thr_curve[1:-1])
    )

    # 字头那一刻能量必须「从低处涨上来」：比较峰前后各 30ms 的平均能量。
    # 不能看 80ms 内的最低值——起音过程中每 10ms 都像「从零涨上来」，会连切好几刀。
    w = max(2, int(round(0.03 / frame_dt)))
    cum = np.concatenate(([0.0], np.cumsum(rms, dtype=np.float64)))
    idx = np.arange(n)
    lo = np.maximum(0, idx - w)
    pre = (cum[idx] - cum[lo]) / np.maximum(1, idx - lo)
    hi = np.minimum(n, idx + 1 + w)
    post = (cum[hi] - cum[idx + 1]) / np.maximum(1, hi - (idx + 1))
    peak_rms = float(np.percentile(rms, 98)) + 1e-9
    rise_ok = (post > pre * 1.30) & ((post - pre) > 0.02 * peak_rms)
    peaks &= rise_ok
    peaks &= mask

    # 按时间顺序取峰，相邻至少 min_onset_gap（按时间比按强弱稳）
    min_gap = max(1e-3, min_onset_gap_ms / 1000.0)
    kept: List[int] = []
    for i in np.flatnonzero(peaks):
        t = float(times[i])
        if not kept or t - float(times[kept[-1]]) >= min_gap:
            kept.append(int(i))
    onset_times = [float(times[i]) for i in kept]

    # ---- 3. 每个发声段内部，用「段首 + 字头」切成音节 ----
    min_syl = max(0.03, min_syllable_ms / 1000.0)
    syllables: List[Syllable] = []
    for s, e in regions:
        rs = float(times[s])
        re_ = float(times[min(e, n - 1)]) + frame / (2.0 * sr)
        if re_ - rs < min_syl:
            continue
        bounds = [rs]
        for t in onset_times:
            if t < rs + 0.45 * min_syl or t > re_ - 0.45 * min_syl:
                continue
            if t - bounds[-1] >= min_syl:
                bounds.append(t)
        for i, b in enumerate(bounds):
            end = bounds[i + 1] if i + 1 < len(bounds) else re_
            if end - b < min_syl:
                if syllables and abs(syllables[-1].end - b) < 1e-3:
                    syllables[-1].end = end      # 尾巴太短，并进上一个字
                continue
            syllables.append(Syllable(b, end))

    if not syllables:
        return []

    # 每个音节中段估基频（对齐时给「一组音里挑哪个」用）
    if with_pitch:
        for syl in syllables:
            syl.midi = _syllable_midi(y, sr, syl.start, syl.end)

    logger.info("检测到 %d 个音节（%.1f ~ %.1f 秒）",
                len(syllables), syllables[0].start, syllables[-1].end)
    return syllables


# ---------------------------------------------------------------- 对齐

def _pitch_class_dist(a: int, b: float) -> float:
    """音高类距离（0~6），不受八度影响。"""
    return abs(((float(a) - float(b)) + 6.0) % 12.0 - 6.0)


def align_notes_to_syllables(
    events: Sequence,
    syllables: Sequence[Syllable],
    tolerance_ms: float = 90.0,
    min_hold_ms: float = 90.0,
) -> Tuple[List[NoteEvent], Dict[str, object]]:
    """把琴谱音符按时间配到音节上：一个音节留一个音。

    返回 (新 events, 统计)。统计里的 `coverage` 是「落在音节内的原始音符占比」，
    界面用它判断音频和琴谱是不是同一首歌（时间轴对不上时会很低）。
    """
    evs = sorted(events, key=lambda e: float(e.time))
    st: Dict[str, object] = {
        "syllables": len(syllables),
        "notes_in": len(evs),
        "notes_out": 0,
        "dropped": 0,
        "split": 0,
        "inside": 0,
        "silent_syllables": 0,
        "coverage": 0.0,
        "with_pitch": sum(1 for s in syllables if s.midi is not None),
    }
    if not evs or not syllables:
        st["notes_out"] = len(evs)
        return evs, st

    # ---- 1. 先按原始音符统计「有没有落在人声里」（界面判断时间轴是否对得上）----
    starts = [float(s.start) for s in syllables]
    tol = max(0.0, tolerance_ms / 1000.0)

    def _covers(e) -> bool:
        t = float(e.time)
        t2 = t + max(float(e.duration or 0.0), 0.01)
        i = bisect.bisect_right(starts, t) - 1
        for j in (i, i + 1):
            if 0 <= j < len(syllables):
                s = syllables[j]
                if t2 >= s.start - tol and t <= s.end + tol:
                    return True
        return False

    covered_orig = sum(1 for e in evs if _covers(e))
    st["inside"] = covered_orig
    st["coverage"] = round(covered_orig / float(len(evs)), 4) if evs else 0.0
    st["dropped"] = len(evs) - covered_orig

    # ---- 2. 跨音节的长音在边界处拆开（同一个音高，分成两段）----
    split_events: List = []
    for e in evs:
        t = float(e.time)
        d = float(e.duration or 0.0)
        if d <= 0.05:
            split_events.append(e)
            continue
        lo = bisect.bisect_right(starts, t + 0.02)
        hi = bisect.bisect_left(starts, t + d - 0.02)
        cuts = starts[lo:hi]
        if not cuts:
            split_events.append(e)
            continue
        pieces: List[Tuple[float, float]] = []
        cur = t
        for c in list(cuts) + [t + d]:
            if c - cur >= 0.02:
                pieces.append((cur, c))
                cur = c
        if len(pieces) <= 1:
            split_events.append(e)
            continue
        for a, b in pieces:
            split_events.append(NoteEvent(
                time=round(a, 4), note=int(e.note), key=e.key, action=e.action,
                duration=round(b - a, 4), lyric=None,
            ))
        st["split"] = int(st["split"]) + (len(pieces) - 1)

    # ---- 3. 每个音归到「时间上最近」的音节 ----
    # 音符起点和检测到的字头通常差几毫秒：贴着字头的音一律算新字，
    # 否则会卡在上一个字尾巴里，导致下一个字没音、还白丢一个音。
    snap = 0.03
    buckets: List[List] = [[] for _ in syllables]
    for e in split_events:
        t = float(e.time)
        i = bisect.bisect_right(starts, t) - 1
        best_i = -1
        best_rank = None
        for j in (i, i + 1):
            if j < 0 or j >= len(syllables):
                continue
            s = syllables[j]
            if abs(t - s.start) <= snap:
                rank = (0, abs(t - s.start))
            elif s.start <= t <= s.end:
                rank = (1, t - s.start)
            elif t < s.start:
                rank = (2, s.start - t)
            else:
                rank = (2, t - s.end)
            if best_rank is None or rank < best_rank:
                best_rank, best_i = rank, j
        if best_i >= 0 and best_rank is not None and best_rank[1] <= tol:
            buckets[best_i].append(e)

    # ---- 4. 一个音节只留一个音；有实测基频时优先挑音高最接近的 ----
    hold = max(0.03, min_hold_ms / 1000.0)
    out: List[NoteEvent] = []
    for syl, group in zip(syllables, buckets):
        if not group:
            st["silent_syllables"] = int(st["silent_syllables"]) + 1
            continue
        if syl.midi is not None:
            def score(e, _s=syl):
                # 音高类距离优先（白键保音级）；同分时看**绝对**距离 ——
                # 转写常把音符整个偏一个八度，音高类距离对此完全瞎，
                # 只有绝对值能挑对八度。
                return (
                    round(_pitch_class_dist(int(e.note), _s.midi), 4),
                    round(abs(float(e.note) - float(_s.midi)), 4),
                    -float(e.duration or 0.0),
                    abs(float(e.time) - _s.start),
                )
        else:
            def score(e, _s=syl):
                return (-float(e.duration or 0.0), abs(float(e.time) - _s.start))
        rep = min(group, key=score)
        st["dropped"] = int(st["dropped"]) + (len(group) - 1)

        t = float(rep.time)
        dur = float(rep.duration or 0.0)
        room = max(0.0, float(syl.end) - t)
        if room >= hold:
            dur = max(dur, hold)
        if room > 0.02:
            dur = min(dur, room)
        out.append(NoteEvent(
            time=round(t, 4), note=int(rep.note), key=rep.key, action=rep.action,
            duration=round(max(0.02, dur), 4), lyric=None,
        ))

    # 未落在任何音节里的音（伴奏 / 噪声 / 毛刺）直接丢弃；同一音节里多出来的音也不弹
    st["notes_out"] = len(out)
    out.sort(key=lambda e: float(e.time))
    return out, st


# ---------------------------------------------------------------- 找现成人声轨

def find_vocal_tracks(out_dir=None) -> List[Path]:
    """找①页人声分离留下的 vocals.wav（新的排前面）。"""
    if out_dir is None:
        return []
    root = Path(out_dir)
    if not root.exists():
        return []
    try:
        items = list(root.rglob("vocals.wav"))
    except OSError:
        return []
    try:
        return sorted(items, key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return items
