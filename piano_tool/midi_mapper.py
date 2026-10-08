"""② MIDI → 21 白键事件映射

读取 MIDI，将每个音符映射到 C4~B6 的 21 个白键，输出 events.json。

关键：本琴只有白键（自然音），若原曲调性带升降号，直接「就近吸附」会把
大量音符挪到相邻半音，旋律音程被破坏（实测某曲白键命中率仅 66%，听起来
完全不像原曲）。因此默认先做**自动移调**：按时长加权搜索 -6~+6 的移调量，
选让最多音符落在本琴白键上的那个（同曲实测 66% → 94%），
旋律轮廓完整保留，只是整体换了个调。

映射策略（按优先级）：
    1. 自动移调到最「白键友好」的调（可用 auto_transpose=False 关闭）
    2. 清理过短音符与极近重复音（转写毛刺）
    3. 整体八度折叠到 [C4, B6] 范围（黑键受限下的物理约束）
    4. 黑键按旋律走向替换为上/下邻白键（走向感知改编；关闭则回退就近吸附）
    5. 保持时间顺序，便于试听

输出 events.json 字段：{time, note, key, action}
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import WHITE_KEYS, WHITE_MIDIS, KEY_BY_MIDI, LOWEST_MIDI, HIGHEST_MIDI
from .models import NoteEvent

logger = logging.getLogger("piano_tool.midi_mapper")

# 白键对应的音级（C D E F G A B）
WHITE_PITCH_CLASSES = {0, 2, 4, 5, 7, 9, 11}


def is_white(midi: int) -> bool:
    return midi in WHITE_MIDIS


def fmt_range(r) -> str:
    """把 [lo, hi] 音高范围格式化成 "33~96"；没有数据返回 "—"。

    空谱子（全被清理掉）不能显示成 "None~None"。
    """
    if not r or r[0] is None:
        return "—"
    return f"{int(r[0])}~{int(r[1])}"


def fold_to_range(midi: int) -> int:
    """把任意 MIDI 音高八度折叠进 [C4, B6]（只换八度，不改音级）。"""
    folded = midi
    while folded < LOWEST_MIDI:
        folded += 12
    while folded > HIGHEST_MIDI:
        folded -= 12
    return folded


def snap_to_white(midi: int) -> Tuple[int, bool]:
    """把任意 MIDI 音高映射到最近的 21 白键之一（含八度折叠）。

    返回 (目标白键 MIDI, 是否发生了替换/折叠)。
    """
    folded = fold_to_range(midi)
    in_range = LOWEST_MIDI <= midi <= HIGHEST_MIDI

    if folded in WHITE_MIDIS:
        replaced = (not in_range) or (folded != midi)
        return folded, replaced

    # 黑键：找最近的白键（两侧等距时 min 取列表第一个 = 下方白键）
    best = min(WHITE_MIDIS, key=lambda w: abs(w - folded))
    return best, True


# 起点差小于此值视为「同时刻」（和弦），不参与走向判断
CONTOUR_CHORD_GAP = 0.02


def snap_sequence_contour(
    times: List[float], pitches: List[int], same_key_gap: float = 0.03
) -> List[int]:
    """走向感知的黑键映射：黑键看旋律走向选上/下邻白键，而不是无脑就近吸附。

    编曲依据：黑键（如 C#）夹在 C 和 D 中间，吸附到哪一边听感完全不同——
    上行经过音取上方白键、下行取下方白键，旋律的走向轮廓才保得住；
    就近吸附会把上行线条「折返」成平台音，听着发卡。

    同时做碰撞规避：优先选择的键在 same_key_gap 内已被前面的音占用时，
    换另一侧（和弦里 C/C# 这类音经常因此被「同键去重」丢掉，换边就能活）。
    同时刻（和弦）的音不参与走向判断；判断不了时回退就近（等距取下方，
    与老版 snap_to_white 的行为一致）。

    times/pitches 一一对应且按时间升序；返回每个音的目标白键 MIDI。
    """
    n = len(pitches)
    folded = [fold_to_range(int(p)) for p in pitches]
    is_black = [f % 12 not in WHITE_PITCH_CLASSES for f in folded]
    targets: List[Optional[int]] = [
        f if not b else None for f, b in zip(folded, is_black)
    ]

    last_hit: Dict[int, float] = {}

    def occupied(key: int, t: float) -> bool:
        prev = last_hit.get(key)
        return prev is not None and (t - prev) < same_key_gap

    for i in range(n):
        t = float(times[i])
        if not is_black[i]:
            last_hit[targets[i]] = t
            continue

        f = folded[i]
        below, above = f - 1, f + 1

        # 走向投票：只看时间上「不同时刻」的前/后一个音（同时刻和弦跳过）
        up = down = 0
        j = i - 1
        while j >= 0 and t - float(times[j]) <= CONTOUR_CHORD_GAP:
            j -= 1
        if j >= 0 and folded[j] != f:
            up, down = (up + 1, down) if folded[j] < f else (up, down + 1)
        j = i + 1
        while j < n and float(times[j]) - t <= CONTOUR_CHORD_GAP:
            j += 1
        if j < n and folded[j] != f:
            up, down = (up + 1, down) if folded[j] > f else (up, down + 1)

        if up > down:
            prefer, other = above, below
        elif down > up:
            prefer, other = below, above
        else:
            # 静态/孤立黑键：与老版「就近吸附」一致，等距取下方
            prefer, other = below, above

        # 碰撞规避：优先侧刚被占用就换另一侧，避免被同键去重丢音
        if occupied(prefer, t) and not occupied(other, t):
            prefer = other
        targets[i] = prefer
        last_hit[prefer] = t

    return [int(tg) for tg in targets]


def estimate_transposition(notes) -> Tuple[int, float]:
    """为一批音符估算最佳移调半音数，使尽可能多的音落在本琴白键上。

    用时长加权（长音更重要），在 -6~+6 半音内搜索；
    命中率相同时优先选择移动量小的。
    返回 (移调半音数, 该移调下的白键命中率 0~1)。
    """
    weights: Counter = Counter()
    for n in notes:
        weights[int(round(n.pitch)) % 12] += max(float(n.end - n.start), 0.05)
    total = sum(weights.values())
    if not total:
        return 0, 0.0

    best_shift, best_rate = 0, -1.0
    for shift in range(-6, 7):
        hit = sum(w for pc, w in weights.items() if (pc + shift) % 12 in WHITE_PITCH_CLASSES)
        rate = hit / total
        better = rate > best_rate + 1e-9 or (
            abs(rate - best_rate) <= 1e-9 and abs(shift) < abs(best_shift)
        )
        if better:
            best_shift, best_rate = shift, rate
    return best_shift, best_rate


def pick_melody(notes, window: float = 0.05, mode: str = "loud") -> List:
    """只保留主旋律：同一 onset 簇内只留一个音。

    mode:
        "loud"  - 取响度最大的音（默认）
        "high"  - 取音高最高的音（skyline，人声旋律常在顶层，抗低音干扰更好）

    参考谱（案例曲电脑按键版.txt）基本是单音旋律，伴奏/和声/低音声部在
    21 键琴上会糊成一团，因此提供这个选项。
    """
    def _key(n):
        if mode == "high":
            return (n.pitch, getattr(n, "velocity", 0))
        return (getattr(n, "velocity", 0), n.pitch)

    picked: List = []
    cluster: List = []
    cluster_start = None
    for n in sorted(notes, key=lambda n: n.start):
        if cluster and float(n.start - cluster_start) > window:
            picked.append(max(cluster, key=_key))
            cluster = []
        if not cluster:
            cluster_start = float(n.start)
        cluster.append(n)
    if cluster:
        picked.append(max(cluster, key=_key))
    return picked


def clean_notes(notes, min_duration: float = 0.06, dedupe_gap: float = 0.06) -> List:
    """去掉过短音符，以及同一音高靠得极近的重复音（转写常见毛刺）。"""
    kept: List = []
    last_time: Dict[int, float] = {}
    for n in sorted(notes, key=lambda n: (n.start, -getattr(n, "velocity", 0))):
        if float(n.end - n.start) < min_duration:
            continue
        prev = last_time.get(int(n.pitch))
        if prev is not None and float(n.start - prev) < dedupe_gap:
            continue
        last_time[int(n.pitch)] = float(n.start)
        kept.append(n)
    return kept


class _NoteLike:
    """把 NoteEvent 适配成 clean_notes/pick_melody 需要的 note 形状。

    events.json 不保存力度，这里给一个常量 64；作 skyline 抽取时
    mode='high' 直接比音高，mode='loud' 力度相同则退化为比音高（取最高音）。
    """

    __slots__ = ("start", "end", "pitch", "velocity", "_ev")

    def __init__(self, ev: "NoteEvent"):
        self._ev = ev
        self.start = float(ev.time)
        self.end = float(ev.time + (ev.duration or 0.0))
        self.pitch = float(ev.note)
        self.velocity = 64.0


def clean_events_keep_melody(
    events: List,
    min_duration: float = 0.06,
    dedupe_gap: float = 0.06,
    melody: bool = False,
    melody_mode: str = "high",
) -> Tuple[List, Dict]:
    """对已经生成的 events.json 做一次「清理 + 保持旋律」。

    适用场景：basic_pitch 常把背景音/噪声误识成极短的米粒音，导致播放卡顿、
    听起来不和谐。这里按阈值（默认 60ms）清掉过短音符，并去掉同一音高紧挨
    着的重复音（转写毛刺）；可选再做 skyline 抽主旋律。

    规则：
        - 只丢弃「明确带时值且小于 min_duration」的音；时值为 None（编辑器
          未设按住时长）的音视为未知，保留，避免误删用户手动调整过的音。
        - 返回的是新的 NoteEvent 列表，time/key/action 不变，duration 按清理
          后的实际时值回写（None 维持 None）。
    返回 (清理后的 events, 统计 dict)。
    """
    total_in = len(events)
    kept: List = []
    last_time: Dict[int, float] = {}
    short_dropped = 0
    dup_dropped = 0
    for e in sorted(events, key=lambda e: (e.time, -(e.duration or 0))):
        if e.duration is not None and e.duration < min_duration:
            short_dropped += 1
            continue
        prev = last_time.get(e.note)
        if prev is not None and (e.time - prev) < dedupe_gap:
            dup_dropped += 1
            continue
        last_time[e.note] = e.time
        kept.append(e)

    melody_dropped = 0
    if melody:
        before = len(kept)
        nk = [_NoteLike(e) for e in kept]
        picked = pick_melody(nk, mode=melody_mode)
        kept = [n._ev for n in picked]
        melody_dropped = before - len(picked)

    kept.sort(key=lambda e: e.time)
    stats = {
        "in": total_in,
        "out": len(kept),
        "short_dropped": short_dropped,
        "dup_dropped": dup_dropped,
        "melody_dropped": melody_dropped,
        "min_duration": min_duration,
        "dedupe_gap": dedupe_gap,
        "melody": bool(melody),
    }
    return kept, stats


def soften_melody_range(
    events: List,
    ceiling: Optional[int] = 84,
    max_leap: Optional[int] = 12,
) -> Tuple[List, Dict]:
    """「高音柔化」：把过高、跳进过硬的音做**八度平移**，让旋律更好听。

    只做 ±12 的整数倍平移，因此**音级(pitch class)与调性完全不变**——
    旋律轮廓不会被改，只是换了个八度。这正是它能"柔化"却不会把曲子改坏
    的原因（区别于黑键吸附那种真正改音高的操作）。

    两步：
      1) 限幅 ceiling：高于 ceiling 的音降八度拉回中音区（不得低于最低音）。
         解决"旋律一直顶在高音区、听着尖/直接"。
      2) 跳进平滑 max_leap：相邻音程大于 max_leap 半音时，把后一个音八度
         平移到离前一个音最近的位置，消掉硬跳（听着"一惊一乍"的根源）。
         同时刻的和弦(time 差 < 30ms)不参与平滑，避免把和弦拆散。

    返回 (新 events, 统计)。ceiling/max_leap 传 None 可单独关闭某一步。
    """
    evs = sorted(events, key=lambda e: e.time)
    ceiling_moved = 0
    leap_smoothed = 0
    prev: Optional[int] = None
    prev_t: Optional[float] = None
    out: List = []

    for e in evs:
        n = int(e.note)

        # 1) 限幅：高于 ceiling 就降八度
        if ceiling is not None:
            while (n > ceiling and (n - 12) >= LOWEST_MIDI
                   and (n - 12) in WHITE_MIDIS):
                n -= 12
        if n != int(e.note):
            ceiling_moved += 1
        after_ceiling = n

        # 2) 跳进平滑（同时刻和弦跳过）
        chord = prev_t is not None and (e.time - prev_t) < 0.03
        if prev is not None and max_leap and not chord:
            guard = 0
            while abs(n - prev) > max_leap and guard < 8:
                cand = [m for d in (-12, 12)
                        if (m := n + d) in WHITE_MIDIS]
                if not cand:
                    break
                best = min(cand, key=lambda m: abs(m - prev))
                if abs(best - prev) >= abs(n - prev):
                    break  # 没有更近的合法位置，停止
                n = best
                guard += 1
        if n != after_ceiling:
            leap_smoothed += 1

        if n != int(e.note):
            out.append(NoteEvent(
                time=e.time,
                note=n,
                key=KEY_BY_MIDI.get(n, e.key),
                action=e.action,
                duration=e.duration,
            ))
        else:
            out.append(e)

        prev = n
        prev_t = e.time

    stats = {
        "in": len(events),
        "out": len(out),
        "ceiling_moved": ceiling_moved,
        "leap_smoothed": leap_smoothed,
        "ceiling": ceiling,
        "max_leap": max_leap,
    }
    return out, stats


def estimate_bpm(times, min_bpm: int = 60, max_bpm: int = 200) -> Optional[float]:
    """从音符起点序列估计曲速（BPM）。

    做法：对每个候选 BPM，看相邻音的间隔有多接近"整拍 / 半拍"，
    取平均对齐误差最小的那个。转写出来的起点总有抖动，但只要整体
    是踩着拍子走的，这个估计就够用（后续量化会把它对齐干净）。
    """
    ts = sorted(float(t) for t in times)
    diffs = [b - a for a, b in zip(ts, ts[1:]) if 0.05 < (b - a) < 2.0]
    if len(diffs) < 3:
        return None
    best_bpm, best_err = None, None
    for bpm in range(min_bpm, max_bpm + 1):
        beat = 60.0 / bpm
        err = 0.0
        for d in diffs:
            u = d / beat
            # 允许对齐到整拍或半拍，取更接近的那个
            err += min(abs(u - round(u)), abs(u * 2 - round(u * 2)) * 0.5)
        err /= len(diffs)
        if best_err is None or err < best_err - 1e-12:
            best_bpm, best_err = bpm, err
    return float(best_bpm)


def organize_notes(
    events: List,
    bpm: Optional[float] = None,
    grid_div: int = 2,
    quantize_time: bool = True,
    quantize_duration: bool = True,
    min_note_beats: float = 0.25,
    max_note_beats: float = 4.0,
    merge_same: bool = True,
) -> Tuple[List, Dict]:
    """「音符整理」：让节奏和时值站到拍子上，听感更规整、更连贯。

    乐理/编曲上的依据（都是 DAW 里的标准做法）：
      1. 节奏量化：转写出的起点总有几毫秒~几十毫秒抖动，听起来"散"。
         对齐到拍网格后节奏才站得住。**只动时间，不动音高**，旋律不受影响。
      2. 时值规整：过短的音（转写碎片）抬到最小单位，过长的音截断，
         避免"米粒音"和"一个音拖半小节"两种极端。
      3. 合并碎片：同一个音被切成好几段时，合并成一个长音（比直接删掉更自然）。
      4. 量化后同音重叠/重复会被合并，避免同一时刻重复触发同一个键。

    grid_div: 一拍切成几格（1=四分音符, 2=八分音符, 4=十六分音符）
    min_note_beats / max_note_beats: 时值下限/上限（单位：**拍**，乘 beat 换算成秒）
    bpm: 为 None 时从音符自动估计。**注意：BPM 只决定网格疏密，不改曲子快慢。**
    返回 (整理后的 events, 统计)。统计里 moved/moved_avg_ms/same_tick 是给
    「参数有没有生效」做证据的 —— 琴谱已在网格上时 moved=0（整理是幂等的）。
    """
    evs = sorted(events, key=lambda e: e.time)
    if not evs:
        return [], {"in": 0, "out": 0, "bpm": None, "grid": None, "merged": 0,
                    "moved": 0, "moved_avg_ms": 0.0, "same_tick": 0}

    onsets = [float(e.time) for e in evs]
    bpm_used = float(bpm) if bpm else (estimate_bpm(onsets) or 120.0)
    beat = 60.0 / bpm_used
    grid = beat / max(1, int(grid_div))
    t0 = onsets[0]

    moved = 0
    moved_total = 0.0
    out: List = []
    for e in evs:
        t = float(e.time)
        if quantize_time:
            t = t0 + round((t - t0) / grid) * grid
            t = max(0.0, round(t, 4))
            if abs(t - float(e.time)) > 1e-6:
                moved += 1
                moved_total += abs(t - float(e.time))
        d = e.duration
        if quantize_duration and d is not None:
            d = round(d / grid) * grid
            # 单位修正：min/max_note_beats 的单位是「拍」，要乘 beat（不是 grid）
            d = max(d, beat * min_note_beats)
            d = min(d, beat * max_note_beats)
            d = round(d, 4)
        out.append(NoteEvent(
            time=t,
            note=e.note,
            key=e.key,
            action=e.action,
            duration=d,
        ))

    merged = 0
    if merge_same:
        kept: List = []
        for e in out:
            if kept:
                prev = kept[-1]
                gap = e.time - prev.time
                if prev.note == e.note and gap < grid * 0.5:
                    # 同一个音被切碎了：合并成一个长音
                    if prev.duration is not None:
                        prev.duration = round(
                            max(prev.duration, gap + (e.duration or 0.0)), 4
                        )
                    merged += 1
                    continue
            kept.append(e)
        out = kept

    out.sort(key=lambda e: e.time)
    # 同一时刻的不同音（量化可能把不同时刻的音拉到同一刻，也可能本来就是和弦）
    from collections import Counter

    tick_counts = Counter(round(e.time, 4) for e in out)
    same_tick = sum(c - 1 for c in tick_counts.values() if c > 1)
    stats = {
        "in": len(events),
        "out": len(out),
        "bpm": round(bpm_used, 1),
        "bpm_estimated": bpm is None,
        "grid": round(grid, 4),
        "grid_div": int(grid_div),
        "merged": merged,
        "quantized_time": bool(quantize_time),
        "quantized_duration": bool(quantize_duration),
        "moved": moved,
        "moved_avg_ms": round(1000.0 * moved_total / moved, 1) if moved else 0.0,
        "same_tick": int(same_tick),
    }
    return out, stats


def _white_hit_rate(notes, shift: int) -> float:
    total = 0.0
    hit = 0.0
    for n in notes:
        w = max(float(n.end - n.start), 0.05)
        total += w
        if (int(round(n.pitch)) + shift) % 12 in WHITE_PITCH_CLASSES:
            hit += w
    return hit / total if total else 0.0


# ---------------------------------------------------------------------------
# 分层独立八度（对标模拟器「启用自动八度偏移（各轨独立优化）」）
# ---------------------------------------------------------------------------
# 为什么需要：只有 21 个白键（60~95，正好 3 个八度）却塞进一个谱子的所有音，
# 低音贝斯会被 snap_to_white 强行八度折叠上来，和旋律挤在同一段 —— 实测真实
# 谱子 56.7% 的音符都是这么来的，听起来就是「糊成一团」。
#
# 分层八度的做法：先把音符切成几组，每组**单独挑一个八度偏移**，让各组尽量
# 落在互不重叠的八度带上（60~71 / 72~83 / 84~95，正好对应键盘三行），
# 于是低音在底行、中音在中行、旋律在顶行，层次就分开了。
LAYER_BY_PITCH = "pitch"   # A：按音高切层（低/中/高三等分）
LAYER_BY_TRACK = "track"   # B：按 MIDI 音轨切层（每个 instrument 独立算八度）
LAYER_MODES = (LAYER_BY_PITCH, LAYER_BY_TRACK)

DEFAULT_LAYER_COUNT = 3    # A 模式的层数
LAYER_SHIFT_CANDIDATES = (-24, -12, 0, 12, 24)


def octave_bands(count: int = DEFAULT_LAYER_COUNT) -> List[List[int]]:
    """把 60~95 切成 count 段互不重叠的八度带，例如 3 段 = [60,71] / [72,83] / [84,95]。"""
    span = HIGHEST_MIDI - LOWEST_MIDI + 1
    size = max(1, span // max(1, count))
    bands = []
    for i in range(count):
        lo = LOWEST_MIDI + i * size
        hi = min(lo + size - 1, HIGHEST_MIDI)
        bands.append([lo, hi])
    return bands


def best_layer_shift(pitches: List[int], band: Optional[List[int]] = None) -> int:
    """给一组音高挑一个整体八度偏移（只换八度、不改音级）。

    band 给了就额外奖励「落进这一层的专属八度带」，于是各层会自动错开、
    互不重叠；band 为 None 时（B 模式）只求落进 21 键范围 + 白键友好。
    """
    if not pitches:
        return 0
    total = float(len(pitches))
    lo, hi = (band if band else [LOWEST_MIDI, HIGHEST_MIDI])
    best_k, best_score = 0, -1e9
    for k in LAYER_SHIFT_CANDIDATES:
        in_range = sum(1 for p in pitches if LOWEST_MIDI <= p + k <= HIGHEST_MIDI)
        in_band = sum(1 for p in pitches if lo <= p + k <= hi)
        white = sum(1 for p in pitches if (p + k) % 12 in WHITE_PITCH_CLASSES)
        score = (in_range / total) * 3.0 + (in_band / total) * 2.0 + (white / total) * 1.0
        if score > best_score:
            best_score, best_k = score, k
    return best_k


def split_notes_by_pitch(notes, count: int = DEFAULT_LAYER_COUNT) -> List[List]:
    """按音高等分成 count 组（第 0 组最低），只按音高算归属、组内保持原顺序。"""
    pitches = [int(round(n.pitch)) for n in notes]
    if not pitches or count <= 1:
        return [list(notes)] if notes else [[] for _ in range(count)]
    lo, hi = min(pitches), max(pitches)
    span = float(hi - lo + 1) or 1.0
    buckets: List[List] = [[] for _ in range(count)]
    for n, p in zip(notes, pitches):
        idx = int((p - lo) / span * count)
        idx = min(max(idx, 0), count - 1)
        buckets[idx].append(n)
    return [b for b in buckets if b] or [list(notes)]


def plan_layer_shifts(groups: List[List],
                      bands: Optional[List[Optional[List[int]]]] = None
                      ) -> List[Tuple[List, int]]:
    """给每一组音符挑一个额外八度偏移，返回 [(组, 偏移)]。

    偏移只换八度、不改音级，所以旋律轮廓不会被破坏，只是把低音区/中音区/
    高音区各自搬到琴上不同的八度带，让它们不再挤在一起。
    """
    bands = bands or [None] * len(groups)
    plan: List[Tuple[List, int]] = []
    for g, band in zip(groups, bands):
        pitches = [int(round(n.pitch)) for n in g]
        plan.append((g, best_layer_shift(pitches, band)))
    return plan


def map_midi_to_events(
    midi_path,
    bpm: int = 120,
    auto_transpose: bool = True,
    transpose: Optional[int] = None,
    min_duration: float = 0.06,
    dedupe_gap: float = 0.06,
    mono: bool = False,
    melody_mode: str = "loud",
    fold_out_of_range: bool = True,
    same_key_gap: float = 0.03,
    contour_black_keys: bool = True,
    layered_octave: bool = False,
    layer_mode: str = LAYER_BY_PITCH,
    layer_count: int = DEFAULT_LAYER_COUNT,
) -> Tuple[List[NoteEvent], Dict]:
    """读取 MIDI 并映射为按键事件列表 + 统计信息。"""
    import pretty_midi

    pm = pretty_midi.PrettyMIDI(str(midi_path))
    raw = [n for inst in pm.instruments for n in inst.notes]

    # ---- 分层独立八度：先切层，每层单独挑一个八度偏移 ----
    # 关掉这个开关（默认）时下面的 groups 只有一层、偏移恒为 0，
    # 走的是和以前一模一样的老流程。
    layered = bool(layered_octave)
    layer_mode = str(layer_mode or LAYER_BY_PITCH)
    if layered and layer_mode not in LAYER_MODES:
        layer_mode = LAYER_BY_PITCH

    if layered and layer_mode == LAYER_BY_TRACK:
        # B：每层 = 一个 MIDI 音轨，各自清理/取旋律后再独立算八度
        groups = []
        for inst in pm.instruments:
            g = clean_notes(inst.notes, min_duration=min_duration,
                            dedupe_gap=dedupe_gap)
            if mono:
                g = pick_melody(g, mode=melody_mode)
            if g:
                groups.append(g)
        bands: List[Optional[List[int]]] = [None] * len(groups)
    else:
        notes_all = clean_notes(raw, min_duration=min_duration, dedupe_gap=dedupe_gap)
        if mono:
            notes_all = pick_melody(notes_all, mode=melody_mode)
        if layered:
            # A：按音高三等分成低/中/高三层
            groups = split_notes_by_pitch(notes_all, layer_count)
            bands = octave_bands(layer_count)[:len(groups)]
        else:
            groups = [notes_all]
            bands = [None]
    if layered:
        layers = plan_layer_shifts(groups, bands)
    else:
        # 没开分层就得和以前一模一样：一层、零偏移，一个字节都不多动
        layers = [(g, 0) for g in groups]
    notes = [n for g, _ in layers for n in g]
    layer_reports: List[dict] = []
    for band, (g, k) in zip(bands, layers):
        pitches = [int(round(n.pitch)) for n in g]
        layer_reports.append({
            "band": band,
            "shift": k,
            "count": len(g),
            "orig_range": [int(min(pitches)), int(max(pitches))] if pitches else None,
            "mapped_range": ([int(min(pitches)) + k, int(max(pitches)) + k]
                             if pitches else None),
        })

    # 转写出来的原始音高范围（移调前）。界面上要展示成
    # 「原始范围 → 移调 N 半音 → 落到琴键范围」，好让用户一眼看出
    # 谱子有没有被整体抬高/压低、有多少音是被八度折叠塞进来的。
    orig_pitches = [int(round(n.pitch)) for n in notes]
    orig_range = [min(orig_pitches), max(orig_pitches)] if orig_pitches else None

    # 移调：显式指定优先，否则自动搜索最「白键友好」的调
    shift, hit_rate = 0, None
    if transpose is not None:
        shift = int(transpose)
        hit_rate = _white_hit_rate(notes, shift)
    elif auto_transpose:
        shift, hit_rate = estimate_transposition(notes)
    if hit_rate is None:
        hit_rate = _white_hit_rate(notes, shift)  # 不移调时也要给出命中率，便于对比

    # 先做音高映射，再去掉「映射到同一键且几乎同时」的重复点击
    # （不同音高被吸附到同一个白键时会出现，实际是同一个键连点两次，纯噪声）
    pending: List[Tuple[float, int, float]] = []
    dropped_range = 0
    for group, layer_k in layers:
        for n in group:
            orig = int(round(n.pitch)) + shift + layer_k
            in_range = LOWEST_MIDI <= orig <= HIGHEST_MIDI
            if not in_range and not fold_out_of_range:
                # 本琴只有 21 键，超音域的多半是低音贝斯；折叠上来的话会突然
                # 出现在旋律音区、听起来很乱，因此提供「直接丢弃」的选项。
                dropped_range += 1
                continue
            pending.append((float(n.start), orig,
                            float(max(0.05, n.end - n.start))))

    # 走向感知黑键改编：黑键按旋律走向选上/下邻白键，并避开刚被占用的键。
    # 必须带时间上下文整段处理（纯函数 snap_to_white 逐音吸附做不到），
    # 关掉则回退到老的逐音「就近吸附」行为。
    contour_redirect = 0
    if contour_black_keys and pending:
        pending.sort(key=lambda it: (it[0], it[1]))
        targets = snap_sequence_contour(
            [p[0] for p in pending], [p[1] for p in pending],
            same_key_gap=same_key_gap,
        )
    else:
        targets = None

    mapped: List[Tuple[float, int, int, bool, float]] = []
    for idx, (t, orig, dur) in enumerate(pending):
        in_range = LOWEST_MIDI <= orig <= HIGHEST_MIDI
        if targets is not None:
            target = targets[idx]
            replaced = (orig % 12 not in WHITE_PITCH_CLASSES) or (not in_range)
            if (orig % 12 not in WHITE_PITCH_CLASSES
                    and target != snap_to_white(orig)[0]):
                contour_redirect += 1
        else:
            target, replaced = snap_to_white(orig)
        mapped.append((t, target, orig, replaced, dur))

    filtered: List = []
    last_hit: Dict[int, float] = {}
    # 去重前先按时间排序：先响的音保留。（分层模式下 groups 是按层给的，
    # 不排序的话「谁活下来」取决于层序而不是时间序）
    mapped.sort(key=lambda it: (it[0], it[2]))
    for item in mapped:
        t, target = item[0], item[1]
        prev = last_hit.get(target)
        if prev is not None and t - prev < same_key_gap:
            continue
        last_hit[target] = t
        filtered.append(item)
    dup_removed = len(mapped) - len(filtered)

    events: List[NoteEvent] = []
    mapped_lo, mapped_hi = None, None
    keys_used = set()
    stats = {
        "total": 0,
        "orig_range": orig_range,      # 转写得到的原始音高范围（移调前）
        "mapped_range": None,          # 最终落进 21 键的范围
        "keys_used": 0,                # 实际用到几个不同的键
        "white_span": [LOWEST_MIDI, HIGHEST_MIDI],
        "raw_total": len(raw),
        "dropped": len(raw) - len(notes) + dup_removed + dropped_range,
        "dropped_range": dropped_range,
        "dup_removed": dup_removed,
        "mono": bool(mono),
        "black_replaced": 0,  # 黑键被吸附到相邻白键的数量（会改变音高）
        "contour": bool(contour_black_keys),   # 是否启用走向感知黑键改编
        "contour_redirect": 0,  # 走向改编中「没有落在就近侧」的黑键数量
        "octave_folded": 0,   # 超出范围被八度折叠的数量（音级不变）
        "white_kept": 0,      # 范围内白键直接保留的数量
        "transpose": shift,
        "white_hit_rate": None if hit_rate is None else round(hit_rate, 4),
        "layered": layered,                       # 是否开了「分层独立八度」
        "layer_mode": layer_mode if layered else None,
        "layers": layer_reports,                  # 每层：目标带 / 八度偏移 / 音符数
    }

    for t, target, orig, replaced, dur in filtered:
        stats["total"] += 1
        mapped_lo = target if mapped_lo is None else min(mapped_lo, target)
        mapped_hi = target if mapped_hi is None else max(mapped_hi, target)
        keys_used.add(target)
        # 注意：是否黑键只看音级(pitch class)，不能看是否落在 21 键音域内，
        # 否则会把「超出音域被八度折叠」的音误算成黑键（两者性质完全不同：
        # 八度折叠保留音级，只是换八度；黑键吸附才会改变音高、破坏旋律）。
        if orig % 12 not in WHITE_PITCH_CLASSES:
            stats["black_replaced"] += 1
        if not (LOWEST_MIDI <= orig <= HIGHEST_MIDI):
            stats["octave_folded"] += 1
        if not replaced and orig in WHITE_MIDIS:
            stats["white_kept"] += 1

        events.append(
            NoteEvent(
                time=t,
                note=target,
                key=KEY_BY_MIDI.get(target, "?"),
                action="press",
                duration=dur,
            )
        )

    # 落位统计要在循环跑完之后才算（mapped_lo/hi/keys_used 在循环里累计）
    stats["mapped_range"] = [mapped_lo, mapped_hi] if mapped_lo is not None else None
    stats["keys_used"] = len(keys_used)
    stats["contour_redirect"] = contour_redirect

    if layered:
        logger.info(
            "分层独立八度（%s）：%s",
            "按音高切层" if layer_mode == LAYER_BY_PITCH else "按音轨切层",
            " / ".join(
                "第%d层 原始%s→%s（带%s，偏移%+d半音，%d音）" % (
                    i + 1,
                    fmt_range(r["orig_range"]), fmt_range(r["mapped_range"]),
                    fmt_range(r["band"]), r["shift"], r["count"],
                )
                for i, r in enumerate(layer_reports)
            ),
        )
    events.sort(key=lambda e: e.time)
    contour_note = f"，走向改编 {contour_redirect}" if contour_black_keys else ""
    logger.info(
        "映射完成：%d 音符（原始 %d，清理 %d）→ 音域 %s → 移调 %+d 半音 → 落到 %s"
        "（琴键 %d~%d，用了 %d 个键），白键命中 %.1f%%，黑键替换 %d%s，八度折叠 %d，白键保留 %d",
        stats["total"], stats["raw_total"], stats["dropped"], fmt_range(orig_range),
        shift, fmt_range(stats["mapped_range"]), LOWEST_MIDI, HIGHEST_MIDI,
        stats["keys_used"], (hit_rate or 0) * 100, stats["black_replaced"],
        contour_note, stats["octave_folded"], stats["white_kept"],
    )
    return events, stats


def build_events_json(events: List[NoteEvent]) -> List[dict]:
    return [
        {
            "time": round(e.time, 3),
            "note": e.note,
            "key": e.key,
            "action": e.action,
            # 时值必须落地，否则"按住时值"播放拿不到长短
            "duration": None if e.duration is None else round(e.duration, 3),
        }
        for e in events
    ]


def write_events(events: List[NoteEvent], out_path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(build_events_json(events), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return out


def export_key_notation(events: List[NoteEvent], phrase_gap: float = 0.6,
                        note_gap: float = 0.18) -> str:
    """导出「按键谱」文本，风格对齐 案例曲电脑按键版.txt。

    - 单音直接写字母（Z X C V B N M / A S D F G H J / Q W E R T Y U）
    - 同一时刻的多个音写成和弦 (HDAN)
    - 间隔较大加空格分句，更大间隔换行分段
    """
    lines: List[str] = []
    cur: List[str] = []
    prev_t: Optional[float] = None
    i = 0
    while i < len(events):
        group = [events[i]]
        j = i + 1
        while j < len(events) and events[j].time - events[i].time < 0.03:
            group.append(events[j])
            j += 1
        t = group[0].time
        if prev_t is not None:
            gap = t - prev_t
            if gap >= phrase_gap:
                lines.append("".join(cur))
                cur = []
            elif gap >= note_gap:
                cur.append(" ")
        if len(group) > 1:
            keys = [g.key for g in sorted(group, key=lambda g: g.note)]
            cur.append("(" + "".join(keys) + ")")
        else:
            cur.append(group[0].key)
        prev_t = t
        i = j
    if cur:
        lines.append("".join(cur))
    return "\n\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m piano_tool.midi_mapper",
        description="MIDI → 21 白键 events.json",
    )
    parser.add_argument("input", help="输入 MIDI 路径")
    parser.add_argument("-o", "--output", default="data/output/events.json")
    parser.add_argument("--bpm", type=int, default=120)
    parser.add_argument("--no-auto-transpose", action="store_true",
                        help="关闭自动移调（默认开启，显著提升像原曲的程度）")
    parser.add_argument("--transpose", type=int, default=None,
                        help="手动指定移调半音数，优先于自动移调")
    parser.add_argument("--min-duration", type=float, default=0.06,
                        help="丢弃短于此秒数的音符（默认 0.06）")
    parser.add_argument("--dedupe-gap", type=float, default=0.06,
                        help="同一音高间隔小于此秒数的重复音只保留一个（默认 0.06）")
    parser.add_argument("--mono", action="store_true",
                        help="只保留主旋律（同一拍内取最响的音），更接近参考按键谱的单音风格")
    parser.add_argument("--melody-mode", choices=["loud", "high"], default="loud",
                        help="配合 --mono：loud=取最响的音，high=取最高音（推荐，更贴近人声旋律）")
    parser.add_argument("--drop-out-of-range", action="store_true",
                        help="超音域的音直接丢弃（默认折叠八度塞进琴键范围）")
    parser.add_argument("--no-contour", action="store_true",
                        help="关闭黑键走向改编，回退为逐音就近吸附")
    parser.add_argument("--notation", default=None,
                        help="额外导出按键谱文本（风格同 案例曲电脑按键版.txt）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    events, stats = map_midi_to_events(
        args.input,
        bpm=args.bpm,
        auto_transpose=not args.no_auto_transpose,
        transpose=args.transpose,
        min_duration=args.min_duration,
        dedupe_gap=args.dedupe_gap,
        mono=args.mono,
        melody_mode=args.melody_mode,
        fold_out_of_range=not args.drop_out_of_range,
        contour_black_keys=not args.no_contour,
    )
    out = write_events(events, args.output)
    rate = stats.get("white_hit_rate")
    print(f"✓ 映射完成：{stats['total']} 音符 -> {out}")
    print(
        f"  移调 {stats['transpose']:+d} 半音 · 白键命中 "
        f"{('%.1f%%' % (rate * 100)) if rate is not None else '—'} · "
        f"黑键替换 {stats['black_replaced']} · "
        f"八度折叠 {stats['octave_folded']} · 白键保留 {stats['white_kept']} · "
        f"清理毛刺 {stats['dropped']}"
    )
    if args.notation:
        p = Path(args.notation)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(export_key_notation(events), encoding="utf-8")
        print(f"✓ 按键谱已导出：{p}")


if __name__ == "__main__":
    main()
