"""② MIDI → 白键事件映射

读取 MIDI，把每个音符映射到**当前乐器**的白键（风物之诗琴 21 键 C4~B6，
首席女高音 14 键 C4~B5），输出 events.json。

当前乐器由 `piano_tool.instruments.active()` 给出（界面在 ② 页选乐器时切它），
音域、键位表、分层八度带全部跟着它走 —— 同一首 MIDI 在两件乐器上会落到
不同的键上，因为同一个字母键在两件乐器上发的音不一样。

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

from .instruments import active
from .models import NoteEvent

logger = logging.getLogger("piano_tool.midi_mapper")

# 白键对应的音级（C D E F G A B）。两件乐器都是 C 大调，这一条与乐器无关。
WHITE_PITCH_CLASSES = {0, 2, 4, 5, 7, 9, 11}


def is_white(midi: int) -> bool:
    return midi in active().white_midis


def fmt_range(r) -> str:
    """把 [lo, hi] 音高范围格式化成 "33~96"；没有数据返回 "—"。

    空谱子（全被清理掉）不能显示成 "None~None"。
    """
    if not r or r[0] is None:
        return "—"
    return f"{int(r[0])}~{int(r[1])}"


def fold_to_range(midi: int) -> int:
    """把任意 MIDI 音高八度折叠进当前乐器的音域（只换八度，不改音级）。"""
    ak = active()
    folded = midi
    while folded < ak.lowest_midi:
        folded += 12
    while folded > ak.highest_midi:
        folded -= 12
    return folded


def snap_to_white(midi: int) -> Tuple[int, bool]:
    """把任意 MIDI 音高映射到最近的琴键之一（含八度折叠）。

    返回 (目标白键 MIDI, 是否发生了替换/折叠)。
    """
    ak = active()
    folded = fold_to_range(midi)
    in_range = ak.lowest_midi <= midi <= ak.highest_midi

    if folded in ak.white_midis:
        replaced = (not in_range) or (folded != midi)
        return folded, replaced

    # 黑键：找最近的白键（两侧等距时 min 取列表第一个 = 下方白键）
    best = min(ak.white_midis, key=lambda w: abs(w - folded))
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


def snap_sequence_vocal(times: List[float], pitches: List[int],
                        comfort_band: Optional[List[int]] = None
                        ) -> List[Optional[int]]:
    """人声 / 单声部乐器的音高落位：**保旋律轮廓**，不做逐音独立折叠。

    为什么不能沿用拨弦乐器那套
    --------------------------
    `snap_sequence_contour` 是先对每个音**各自**做八度折叠再吸附，这在多声部
    键盘乐器上没问题（各声部本来就在不同八度），但放在**单声部人声**上会致命：
    旋律走到音域边界时，上一个音还在 B5、下一个音被独立折叠成 C4 —— 听感就是
    突然劈嗓子/破音，正是用户说的「耳朵疼」。人声要的是**整条线连起来**。

    做法（只用 ±12 的整数倍平移，音级完全不变）
    --------------------------------------------
      1. 第一个音：在所有合法八度里挑离原音高最近的那个（把旋律整体放进音域）
      2. 后面的音：挑**让「实际音程」最接近「原曲音程」**的那个八度。
         例如原曲 C4→C6（+24 半音），本琴只有两个八度，就落到 C4→C5（+12），
         保住「向上跳一个八度」的走向，而不是变成同音或反向大跳。
      3. 黑键（音级不在白键里）：在它上下邻白键里挑音程最接近的那个
      4. 实在放不进音域返回 None，由调用方当「丢音」处理

    返回与 pitches 等长的 list[Optional[int]]。
    """
    ak = active()
    lo, hi = ak.lowest_midi, ak.highest_midi
    whites = set(ak.white_midis)
    center = ((comfort_band[0] + comfort_band[1]) / 2.0) if comfort_band else (lo + hi) / 2.0

    out: List[Optional[int]] = []
    prev_placed: Optional[int] = None
    prev_orig: Optional[int] = None
    prev_t: Optional[float] = None

    for p, t in zip(pitches, times):
        p = int(p)
        # 所有合法落点（同音级 / 黑键取邻白键）
        cands: List[int] = []
        if p % 12 in WHITE_PITCH_CLASSES:
            for k in range(-6, 7):
                m = p + 12 * k
                if lo <= m <= hi and m in whites:
                    cands.append(m)
        else:
            for k in range(-6, 7):
                m = p + 12 * k
                for d in (-1, 1):
                    n = m + d
                    if lo <= n <= hi and n in whites:
                        cands.append(n)
        cands = sorted(set(cands))
        if not cands:
            out.append(None)
            prev_placed = None
            prev_orig = None
            prev_t = None
            continue

        chord = (prev_t is not None and (t - prev_t) <= CONTOUR_CHORD_GAP)
        if prev_placed is None or prev_orig is None or chord:
            # 第一个音（或和弦内）：先按「离原音高最近」把旋律放进音域，
            # 同距离时偏向舒服音区
            best = min(cands, key=lambda c: (abs(c - p), abs(c - center)))
        else:
            # 关键：比较「实际音程」与「原曲音程」的差，而不是「离前一个音多近」。
            # 后者会把大跳压成同音，旋律走向就没了。
            want = p - prev_orig
            best = min(cands, key=lambda c: (abs((c - prev_placed) - want), abs(c - p)))
        out.append(best)
        prev_placed, prev_orig, prev_t = best, p, t

    return out


def humanize_articulation(
    events: List,
    min_hold: Optional[float] = None,
    legato_gap: Optional[float] = None,
    max_gap: Optional[float] = None,
    same_pitch_overlap: Optional[float] = None,
) -> Tuple[List, Dict]:
    """人声「唱法」处理：把音符的时值和连断改得符合这个乐器真实的发声方式。

    为什么不能沿用拨弦那套时值
    --------------------------
    拨弦乐器一碰就响、很快就衰减，所以琴谱写成「短点击」没问题；人声不是：

      · **起音慢**（实测到峰值 35ms，低音最长 150ms）→ 时值短于 ~90ms 的只会
        听成一声气口，根本不成音；必须给一个最小按住时值。
      · **延音长**（实测自然衰减到 -20dB 要约 0.94 秒）→ 人声是「撑住」的，
        两个音之间本该连着；逐音断开重弹等于把一句歌词拆成一个个字往外蹦。
      · **一个音里不能再起一次音**：同一个音还在响时又触发一次，人声做不到，
        听感是「卡了一下」，必须并成一个长音。
      · **换气要留**：但不能拿延音把真正该断的地方糊过去，否则断句就没了。

    四条规则（都只改时值 / 相邻连接方式，**不改任何音高**）：
      0. **同音叠响合并**：同一个音还没结束又被触发（真叠着响）→ 并成一个长音。
         只并「叠着的」；前一个已结束、紧接着唱下一个同音（参考谱里的 `Q Q`）是正常的换字，保留
      1. **最小按住**：时值不足 min_hold 的补到 min_hold（保留短音，不丢你谱子里的音）
      2. **连唱**：与下一个音的空隙小于 legato_gap 时，把这个音延长到下一个音的起点，
         两个音合成一条连续的线（听感是「唱下来」而不是「一个个弹」）
      3. **换气**：空隙超过 max_gap 的原样保留，不许拿延音跨过去

    传入 None 时按当前乐器的发声学参数取（拨弦类没有这些参数 → 原样返回，零改动）。
    """
    ak = active()
    if min_hold is None:
        min_hold = (ak.min_hold_ms / 1000.0) if ak.min_hold_ms else 0.0
    if legato_gap is None:
        legato_gap = (ak.legato_gap_ms / 1000.0) if ak.legato_gap_ms else 0.0
    if max_gap is None:
        max_gap = (ak.articulation_max_gap_ms / 1000.0) if ak.articulation_max_gap_ms else 0.0
    if same_pitch_overlap is None:
        same_pitch_overlap = ((ak.same_pitch_overlap_ms or 0.0) / 1000.0)
    if not min_hold and not legato_gap and not same_pitch_overlap:
        return list(events), {"min_hold": 0.0, "legato": 0, "breathe": 0,
                              "stretched": 0, "trimmed": 0, "merged_same": 0,
                              "in": len(events), "out": len(events)}

    evs = sorted(events, key=lambda e: e.time)
    st = {"in": len(evs), "min_hold": round(min_hold, 3), "stretched": 0,
          "legato": 0, "breathe": 0, "trimmed": 0, "merged_same": 0, "out": 0}

    # ---- 第 0 步：同音「叠着响」的先合并 ----
    # 必须放在时值处理之前：先把「同一个音被切成两段」并回去，
    # 后面算连唱/换气时面对的才是真实的一句旋律。
    # 判据是**真重叠**（start 落在前一个音还没结束的时候），不是「挨得近」——
    # 参考谱里有 `Q Q` 这种前一个唱完、紧接着再唱同一个音的写法，那是正常换字。
    merged: List = []
    for e in evs:
        start = float(e.time)
        dur = e.duration if e.duration is not None else min_hold
        if merged:
            prev = merged[-1]
            prev_end = float(prev.time) + float(prev.duration or 0.0)
            if e.note == prev.note and start < prev_end - same_pitch_overlap:
                # 上一个同音还没结束又被触发 → 并成长音，不重新起音
                prev.duration = round(
                    max(float(prev.duration or 0.0), start + dur - float(prev.time)), 4)
                st["merged_same"] += 1
                continue
        merged.append(NoteEvent(
            time=e.time, note=e.note, key=e.key, action=e.action,
            duration=(None if e.duration is None else round(float(e.duration), 4)),
            lyric=getattr(e, "lyric", None),
        ))

    # ---- 第 1~3 步：最短按住 / 连唱 / 换气 ----
    out: List = []
    leave = 0.008   # 两个音之间至少留 8ms，免得前一音盖住后一音
    for i, e in enumerate(merged):
        nxt = merged[i + 1] if i + 1 < len(merged) else None
        start = float(e.time)
        dur = e.duration
        room = (float(nxt.time) - start) if nxt is not None else None

        # 1) 最小按住：时值不足 min_hold 就补到 min_hold。
        #    但不能超过「到下一个音的距离」——人声一次只能唱一个音。
        #    duration 为 None 的音也要补：播放器对 None 只按 hold_min(30ms) 弹，
        #    对人声来说等于没唱出来。
        want = min_hold
        if room is not None:
            want = min(min_hold, max(leave, room - leave))
        if dur is None or dur < want:
            dur = want
            st["stretched"] += 1

        if nxt is not None:
            gap = room - dur
            if gap > max_gap:
                st["breathe"] += 1          # 真停顿：保留，别用延音糊过去
            elif gap <= legato_gap:
                # 空隙很小（含原谱本身重叠）：把这个音一直连到下一个音的起点，
                # 中间不留气口 —— 人声是「唱下来」的，不是一个个字母往外蹦。
                target = max(leave, room - leave)
                if target < dur:
                    st["trimmed"] += 1      # 原来是重叠的，让位给后一个音
                elif target > dur:
                    st["legato"] += 1       # 原来有小气口，补成连音
                dur = target
        out.append(NoteEvent(
            time=e.time, note=e.note, key=e.key, action=e.action,
            duration=(None if dur is None else round(max(0.0, dur), 4)),
            lyric=getattr(e, "lyric", None),
        ))
    st["out"] = len(out)
    return out, st


def estimate_transposition(notes) -> Tuple[int, float]:
    """为一批音符估算最佳移调半音数，使尽可能多的音落在本琴白键上。

    用时长加权（长音更重要），在 -6~+6 半音内搜索；
    命中率相同时优先选择移动量小的。
    返回 (移调半音数, 该移调下的白键命中率 0~1)。

    注意：**这里只管「调」，不管「音区」**。把人声放进舒服音区是另一件事，
    用 ±12 的整数倍做（见 map_midi_to_events 里的 octave_shift），
    否则会出现「E5 被降成 C5」这种既换调又换音区的双重改动。
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
        "vocal" - 取离上一个已接受音**最近**的音（首席女高音专用）：
                  人声线是连续的，同一簇里的候选要跟前面唱到的那个音接轨，
                  才不会被伴奏/和声的尖音带跑；第一个簇没有上下文，
                  退回 skyline 取最高音。多声部乐器不应用此模式。

    参考谱（案例曲电脑按键版.txt）基本是单音旋律，伴奏/和声/低音声部在
    21 键琴上会糊成一团，因此提供这个选项。
    """

    def _key(n):
        if mode == "high":
            return (n.pitch, getattr(n, "velocity", 0))
        return (getattr(n, "velocity", 0), n.pitch)

    def _best(cluster, last_pitch):
        if mode == "vocal" and last_pitch is not None:
            return min(cluster, key=lambda x: (abs(float(x.pitch) - last_pitch),
                                               -float(x.pitch)))
        return max(cluster, key=_key)

    picked: List = []
    cluster: List = []
    cluster_start = None
    last_pitch = None
    for n in sorted(notes, key=lambda n: n.start):
        if cluster and float(n.start - cluster_start) > window:
            best = _best(cluster, last_pitch)
            picked.append(best)
            last_pitch = float(best.pitch)
            cluster = []
        if not cluster:
            cluster_start = float(n.start)
        cluster.append(n)
    if cluster:
        picked.append(_best(cluster, last_pitch))
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


class _YieldingNote:
    """「低音优化」把伴奏音裁剪到旋律间隙内时的副本（只改 end，不改音高）。

    保留 `_orig` 指向原音符：分层 B 模式（按音轨）要靠它把副本归回原音轨。
    """

    __slots__ = ("start", "end", "pitch", "velocity", "_orig")

    def __init__(self, note, end: float):
        self.start = float(note.start)
        self.end = float(end)
        self.pitch = float(note.pitch)
        self.velocity = getattr(note, "velocity", 64)
        self._orig = note


def _melody_chain_ids(notes, cluster_window: float = 0.05,
                      gap: float = 0.05,
                      max_drop_semitones: Optional[float] = None) -> set:
    """天际线旋律链：找出「旋律线」上的音符，返回其 id 集合。

    max_drop_semitones：**可选**的「起链音高下限」。给了以后，如果某个音
    比上一条链的最后一个音低超过这么多半音，就不允许它另起一条链。
    用来挡掉「只有低音起音的时刻被当成旋律」——例如旋律在 78(F#5)、
    4.7 秒只有一个 49(C#3) 的低音起音，旧逻辑会把它当旋律抬到 72，
    听感就是凭空冒出一个不相干的高音。默认 None = 老行为不变。

    为什么不能只用「每个起音簇取最高音」（pick_melody）
    ------------------------------------------------
    那个判据会把**夹在旋律缝隙里的独奏伴奏音**也当成旋律：贝斯在旋律休止的
    间隙里独奏时，它自己一个簇、当然是簇内最高 → 被当成旋律保留 → 照样糊。
    参考谱的形态是「一条连贯的旋律线」，要用**链**来刻画：

      · **起链**：音未被使用、起音瞬间无更高音在响、cluster_window 内它是最高音、
        且发声区间不与已有链上音重叠（链尾之后的音天然不重叠，所以连音/上行
        都不受影响；能插进链中间的都是被延链跳过、压在旋律下面的伴奏音）；
      · **延链**：在「前一音的起音~前一音结束+gap」区间内起音的未使用音里
        取**最高**的接上（人声旋律常在顶层，与 pick_melody mode='high' 同判据）。

    这样旋律的上下行、连音重叠都不丢音；而旋律下方的贝斯/垫既进不了链
    （同一时刻总有更高的旋律音压着），也起不了新链（起音时被旋律盖住、
    或发声区间压在链中间）。
    """
    import bisect

    n = len(notes)
    starts = [float(x.start) for x in notes]
    ends = [float(x.end) for x in notes]
    pitches = [float(x.pitch) for x in notes]

    # 起音瞬间是否被更高音盖住（扫掠线：按 (start, -pitch) 排序后，
    # 同起点的高音先入活跃表，天然先盖住低音）
    masked = [False] * n
    active: List[Tuple[float, float]] = []   # (end, pitch)
    for i in range(n):
        s = starts[i]
        if active:
            active = [a for a in active if a[0] > s + 1e-9]
        masked[i] = any(p > pitches[i] for _, p in active)
        active.append((ends[i], pitches[i]))

    used = [False] * n
    chain: set = set()
    chain_spans: List[List[float]] = []      # 已合并的链上发声区间（起链避让用）
    last_chain_pitch: Optional[float] = None  # 上一条链的最后一个音（起链下限用）

    def _overlaps_chain(s: float, e: float) -> bool:
        for lo, hi in chain_spans:
            if lo < e and s < hi:
                return True
        return False

    i = 0
    while i < n:
        if used[i] or masked[i]:
            i += 1
            continue
        # 起链条件一：cluster_window 内最高音
        lo = bisect.bisect_left(starts, starts[i] - cluster_window)
        hi = bisect.bisect_right(starts, starts[i] + cluster_window)
        if any(pitches[j] > pitches[i] for j in range(lo, hi)):
            i += 1
            continue
        # 起链条件二：不与已有链上音重叠（挡掉被延链跳过的伴奏音）
        if _overlaps_chain(starts[i], ends[i]):
            i += 1
            continue
        # 起链条件三（可选）：比上一条链低太多 → 这是低音，不是旋律
        if (max_drop_semitones is not None
                and last_chain_pitch is not None
                and pitches[i] < last_chain_pitch - max_drop_semitones):
            i += 1
            continue
        # 起链并延链
        cur = i
        used[cur] = True
        last_chain_pitch = pitches[cur]
        chain.add(id(notes[cur]))
        chain_spans.append([starts[cur], ends[cur]])
        chain_spans.sort()
        while True:
            win_hi = bisect.bisect_right(starts, ends[cur] + gap)
            win_lo = bisect.bisect_right(starts, starts[cur])
            best = None
            for j in range(win_lo, win_hi):
                if used[j]:
                    continue
                if best is None or pitches[j] > pitches[best]:
                    best = j
            if best is None:
                break
            used[best] = True
            last_chain_pitch = pitches[best]
            chain.add(id(notes[best]))
            chain_spans.append([starts[best], ends[best]])
            chain_spans.sort()
            cur = best
        i += 1
    return chain


def optimize_low_voice(
    notes,
    cluster_window: float = 0.05,
    pad: float = 0.03,
    min_keep: float = 0.04,
    lift_to: Optional[Tuple[int, int]] = None,
) -> Tuple[List, Dict]:
    """「低音优化」：非旋律声部避让旋律，只在旋律休息的间隙里保留。

    为什么要这个功能
    ----------------
    自动转写会把一首歌的**所有声音**都变成音符：旋律、贝斯、钢琴和弦、合成器
    铺底……本琴只有 21 个白键，把这些声音原样映射上去，低音/伴奏就会和旋律挤在
    同一段音域里同时响，听感「糊成一团」。而人工演奏谱（用户提供的参考谱）几乎
    都是**单音旋律线**：旋律优先，低音/伴奏要么不弹，要么只在旋律休息的间隙里
    补几个音。这个功能就是按这个原则自动整理，**不针对任何一首具体歌曲**。

    做法（只做「丢/留 + 裁剪时值」，**不改任何音高、不改调**）
    ---------------------------------------------------------
      1. **旋律线**：天际线链（见 _melody_chain_ids）—— 从最高音开始，
         每次接上「前一音结束前起音的音里最高的那个」，连成一条连贯的旋律。
      2. **避让**：其余音（低音贝斯 / 和弦垫 / 伴奏）若起音落在旋律的发声区间里
         → 丢弃（不允许盖住旋律）。
      3. **间隙保留**：起音落在旋律休息间隙里的伴奏音保留，并把时值裁剪到间隙内
         —— 它铃不到下一个旋律音身上。超音域的低音随后按老规则八度折叠进来，
         落在底排，给编曲补一点低音（人工谱里低音也是这么用的）。

    返回 (保留的 notes, 统计 dict)。
    """
    notes = sorted(notes, key=lambda x: (float(x.start), -float(x.pitch)))
    st = {"in": len(notes), "out": 0, "melody": 0, "dropped_overlap": 0,
          "dropped_chain": 0, "kept_gap": 0, "dropped_short": 0,
          "cluster_window": cluster_window, "pad": pad}
    if not notes:
        return list(notes), st

    # 1) 旋律线（天际线链）
    chain_ids = _melody_chain_ids(notes, cluster_window=cluster_window)
    melody = [x for x in notes if id(x) in chain_ids]
    melody.sort(key=lambda x: float(x.start))

    # 1b) 旋律链内避让：链上前后重叠的音（实测转写里 45% 的相邻音都重叠，
    #     转写常把音的结尾写长），先起音的让位 —— 结束点裁到后一个音的起点前，
    #     得到「一个音奏完下一个才起」的单音线（参考谱就是这个形态）。
    #     若裁完不足 min_keep（前音太短），说明后音是钻进来的伴奏/拆分音，丢后音。
    drop_later: set = set()
    for i in range(len(melody) - 1):
        a, b = melody[i], melody[i + 1]
        if float(b.start) < float(a.end) - 1e-9:
            if float(b.start) - pad - float(a.start) < min_keep:
                drop_later.add(i + 1)
    yielded: List = []
    for i, x in enumerate(melody):
        if i in drop_later:
            st["dropped_chain"] += 1        # 钻进来的伴奏/拆分音，丢
            continue
        end = float(x.end)
        if i + 1 < len(melody) and (i + 1) not in drop_later \
                and float(melody[i + 1].start) < end:
            end = float(melody[i + 1].start) - pad
            if end - float(x.start) < min_keep:
                st["dropped_chain"] += 1    # 裁光了（理论上已被上面拦截）→ 丢自己
                continue
        if end < float(x.end) - 1e-6:
            x = _YieldingNote(x, end)
        yielded.append(x)
    melody = yielded
    st["melody"] = len(melody)

    # 2) 旋律发声区间（前后各留 pad）→ 合并 → 空闲间隙 = 补集
    busy = sorted((float(x.start) - pad, float(x.end) + pad) for x in melody)
    merged: List[List[float]] = []
    for lo, hi in busy:
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    free: List[List[float]] = []
    prev_end = 0.0
    for lo, hi in merged:
        if lo > prev_end:
            free.append([prev_end, lo])
        prev_end = max(prev_end, hi)
    free.append([prev_end, float("inf")])   # 尾音之后的间隙（含最后一个旋律音的衰减尾）

    # 3) 逐音判定：旋律音全留；伴奏音起音落在间隙里就留下（裁剪到间隙内），否则丢
    kept: List = list(melody)
    for n in notes:
        if id(n) in chain_ids:
            continue
        s = float(n.start)
        win = None
        for w0, w1 in free:
            if w0 <= s < w1:
                win = (w0, w1)
                break
        if win is None:
            st["dropped_overlap"] += 1     # 起音就在旋律发声区里 → 丢
            continue
        new_end = min(float(n.end), win[1])
        if new_end - s < min_keep:
            st["dropped_short"] += 1        # 裁剪后太短，弹不出来 → 丢
            continue
        if new_end < float(n.end) - 1e-6:
            n = _YieldingNote(n, new_end)   # 裁剪到间隙内，铃不进下一个旋律音
        kept.append(n)
        st["kept_gap"] += 1

    kept.sort(key=lambda x: (float(x.start), float(x.pitch)))

    # 4) 可选：把整条旋律抬到中高音区（避免「旋律本身音区低 → 全落在底排听起来
    #    像低音」）。只换八度、不改音级/调，和「低音优化」单音线条的目标一致。
    #    以旋律线（链上音）的中位音高为基准，整体平移若干八度，使它落进 lift_to
    #    区间；伴奏间隙音随旋律一起平移，保持相对音程不变。
    if lift_to and melody:
        lo_c, hi_c = lift_to
        mp = sorted(float(x.pitch) for x in melody)
        med = mp[len(mp) // 2]
        oct_shift = 0
        guard = 0
        while med + oct_shift < lo_c and guard < 6:
            oct_shift += 12
            guard += 1
        guard = 0
        while med + oct_shift > hi_c and guard < 6:
            oct_shift -= 12
            guard += 1
        if oct_shift:
            for n in kept:
                n.pitch = float(n.pitch) + oct_shift
                orig = getattr(n, "_orig", None)
                if orig is not None:
                    orig.pitch = float(orig.pitch) + oct_shift
            st["lifted_semitones"] = oct_shift

    st["out"] = len(kept)
    return kept, st


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
    ceiling: Optional[int] = None,
    max_leap: Optional[int] = None,
) -> Tuple[List, Dict]:
    """「高音柔化」：把过高、跳进过硬的音做**八度平移**，让旋律更好听。

    ceiling / max_leap 传 None 时按当前乐器的预设取：
      · 人声（首席女高音）→ 上限 = 舒服音区顶 G5(79)、最大跳进 7 半音
      · 其他（风物之诗琴）→ 上限 C6(84)、最大跳进 12 半音（老默认不变）

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
    ak = active()
    if ceiling is None:
        ceiling = (int(ak.soft_ceiling) if ak.soft_ceiling
                   else (int(ak.comfort_band[1]) if ak.comfort_band else 84))
    if max_leap is None:
        max_leap = int(ak.comfort_max_leap) if ak.comfort_max_leap else 12
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
            while (n > ceiling and (n - 12) >= ak.lowest_midi
                   and (n - 12) in ak.white_midis):
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
                        if (m := n + d) in ak.white_midis]
                if not cand:
                    break
                best = min(cand, key=lambda m: abs(m - prev))
                # 两条护栏（否则「柔化」会把旋律改坏）：
                #   · 不能把跳进压成同音 —— 八度大跳变成原地不动，比原来更难听
                #   · 不能把上行跳改成下行跳 —— 走向反了，旋律线就散了
                if abs(best - prev) < 2:
                    break
                if (best - prev) * (n - prev) < 0:
                    break
                if abs(best - prev) >= abs(n - prev):
                    break
                n = best
                guard += 1
        if n != after_ceiling:
            leap_smoothed += 1

        if n != int(e.note):
            out.append(NoteEvent(
                time=e.time,
                note=n,
                key=ak.key_by_midi.get(n, e.key),
                action=e.action,
                duration=e.duration,
                lyric=getattr(e, "lyric", None),
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
    ak = active()
    # 人声：时值不按网格取整。人声的「唱法」（最短按住 / 连唱 / 换气）是实测出来的，
    # 按 1/8 网格取整会把 90ms 的连唱变成 250ms、把换气抹平 —— 那正是「一个个音蹦」的根源。
    # 起点仍然量化（节奏要稳），只保留时值不动。
    quantize_duration = bool(quantize_duration) and not ak.monophonic
    out: List = []
    for e in evs:
        t = float(e.time)
        if quantize_time:
            tq = t0 + round((t - t0) / grid) * grid
            tq = max(0.0, round(tq, 4))
            # 人声守卫：量化不能把两个音吸到**同一刻** —— 一次只能唱一个音，
            # 撞在一起的那个音会被压成 8ms 的「咔哒」。撞车时保留原时刻。
            if ak.monophonic and out and tq <= out[-1].time + 1e-9:
                tq = round(t, 4)
            if abs(tq - float(e.time)) > 1e-6:
                moved += 1
                moved_total += abs(tq - float(e.time))
            t = tq
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
            lyric=getattr(e, "lyric", None),
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

    # 人声：起点量化以后，相邻音的距离变了，连唱/换气要按新时间重算一遍，
    # 否则会出现「量化把两个音拉近了，但时值还是旧的、中间留了个气口」。
    articulation = None
    if ak.min_hold_ms:
        out, articulation = humanize_articulation(out)

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
        "articulation": articulation,
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

# 「声部整理方式」（新增功能）：忠实保留全曲，按声部分到不同八度。
#   track = 独立分轨（按音轨/单轨时按音高，推荐）
#   pitch = 按音高分三段（低/中/高）
#   raw   = 原样多声部（不整理，保留旧行为/旧预设兼容）
#   piano = 钢琴曲适配（按声部角色编配：旋律/低音/和声，见 piano_arrange.py）
#   none  = 不使用本功能，沿用 layered_octave + layer_mode 老参数（老行为）
ARRANGE_MODES = ("none", "track", "pitch", "raw", "piano")

DEFAULT_LAYER_COUNT = 3    # 默认层数（风物之诗琴的排数）；实际按当前乐器的排数走
LAYER_SHIFT_CANDIDATES = (-24, -12, 0, 12, 24)


def octave_bands(count: Optional[int] = None) -> List[List[int]]:
    """分层用的八度带 —— **一排琴键一段**。

    风物之诗琴 3 排 → [60,71] / [72,83] / [84,95]（与老算法结果完全一致）；
    首席女高音 2 排 → [60,71] / [72,83]。

    为什么改成「按排切」：这个功能的用意就是让低音/中音/高音各自落到键盘上
    对应的那一行；老的「按音域等分」在 3 排 21 键时恰好等于按排切，但换到
    2 排 14 键就会切成 8 个半音一段、和键盘行对不上。
    count 大于琴排数时才回退到「按音域等分」补齐（兜底，一般不用传）。
    """
    ak = active()
    wanted = int(count) if count else ak.row_count
    if wanted <= ak.row_count:
        return ak.octave_bands()
    span = ak.highest_midi - ak.lowest_midi + 1
    size = max(1, span // max(1, wanted))
    bands = []
    for i in range(wanted):
        lo = ak.lowest_midi + i * size
        bands.append([lo, min(lo + size - 1, ak.highest_midi)])
    return bands


def best_layer_shift(pitches: List[int], band: Optional[List[int]] = None,
                     legacy: bool = False) -> int:
    """给一组音高挑一个整体八度偏移（只换八度、不改音级）。

    legacy=True：**老版本 A/B 的原始公式**（2026-11-02 恢复）：
        score = 进音域比例×3 + 进带比例×2 + 白键比例×1
      band=None 时“进带”就是“进音域”，等价于 5×进音域 + 1×白键。
      这个公式会把 25 半音宽的左手轨抬两个八度，但用户的 A/B 就是按它
      调好的，必须一字不差地还原。

    legacy=False：新功能（独立分轨 / 按音高三分）用「中位音进带」：
      只抬一个八度，避免和弦高音全挤进旋律区。band=None 时只求
      尽量进音域 + 白键友好。
    """
    if not pitches:
        return 0
    ak = active()
    total = float(len(pitches))

    # ---- 老版本 A/B：原样公式 ----
    if legacy:
        lo, hi = (band if band else [ak.lowest_midi, ak.highest_midi])
        best_k, best_score = 0, -1e9
        for k in LAYER_SHIFT_CANDIDATES:
            in_range = sum(
                1 for p in pitches if ak.lowest_midi <= p + k <= ak.highest_midi)
            in_band = sum(1 for p in pitches if lo <= p + k <= hi)
            white = sum(
                1 for p in pitches if (p + k) % 12 in WHITE_PITCH_CLASSES)
            score = ((in_range / total) * 3.0
                     + (in_band / total) * 2.0
                     + (white / total) * 1.0)
            if score > best_score:
                best_score, best_k = score, k
        return best_k

    # ---- 新行为：band=None 时只求进音域 + 白键友好 ----
    if not band:
        lo, hi = ak.lowest_midi, ak.highest_midi
        best_k, best_score = 0, -1e9
        for k in LAYER_SHIFT_CANDIDATES:
            in_range = sum(1 for p in pitches if lo <= p + k <= hi)
            white = sum(1 for p in pitches if (p + k) % 12 in WHITE_PITCH_CLASSES)
            score = (in_range / total) * 3.0 + (white / total) * 1.0
            if score > best_score:
                best_score, best_k = score, k
        return best_k

    # ---- 新行为（独立分轨 / 按音高三分）：中位音进带 ----
    lo, hi = band
    center = (lo + hi) / 2.0
    ordered = sorted(int(round(p)) for p in pitches)
    med = ordered[len(ordered) // 2]
    candidates = [k for k in LAYER_SHIFT_CANDIDATES if lo <= med + k <= hi]
    if not candidates:
        candidates = list(LAYER_SHIFT_CANDIDATES)

    best_k, best_score = 0, -1e9
    for k in candidates:
        in_range = sum(1 for p in pitches if ak.lowest_midi <= p + k <= ak.highest_midi)
        white = sum(1 for p in pitches if (p + k) % 12 in WHITE_PITCH_CLASSES)
        dist = abs((med + k) - center)
        score = (-dist * 3.0
                 - abs(k) / 12.0 * 0.4
                 + (in_range / total) * 2.0
                 + (white / total) * 1.0)
        if score > best_score or (
                abs(score - best_score) <= 1e-9 and abs(k) < abs(best_k)):
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
                      bands: Optional[List[Optional[List[int]]]] = None,
                      legacy: bool = False
                      ) -> List[Tuple[List, int]]:
    """给每一组音符挑一个额外八度偏移，返回 [(组, 偏移)]。

    偏移只换八度、不改音级，所以旋律轮廓不会被破坏，只是把低音区/中音区/
    高音区各自搬到琴上不同的八度带，让它们不再挤在一起。

    legacy=True 时走老版本 A/B 的原始公式（见 best_layer_shift）。
    """
    bands = bands or [None] * len(groups)
    plan: List[Tuple[List, int]] = []
    for g, band in zip(groups, bands):
        pitches = [int(round(n.pitch)) for n in g]
        plan.append((g, best_layer_shift(pitches, band, legacy=legacy)))
    return plan


def _reduce_polyphony(notes, max_voices: int = 4) -> List:
    """降低同时发声的音数，让音符分明、跟得上节奏（**只减音数，不改任何起音/时值**）。

    basic-pitch 常把一首歌唱成十几种音同时响（八度重叠、滚奏被拆成多个音、填充音），
    忠实全曲映射后就糊成「一直在同时按」。这里按时间扫，每个时刻最多留 max_voices
    个音：**永远保留最高（旋律）和最低（贝斯）**，中间只留少数，其余当 filler 去掉。
    因为不动起音/时值，节奏和原曲完全一致；旋律（最高音）也因此浮出来、不再被埋。
    """
    notes = sorted(notes, key=lambda n: (float(n.start), -float(n.pitch)))
    kept: List = []
    for n in notes:
        t = float(n.start)
        active = [k for k in kept if float(k.start) <= t < float(k.end) + 1e-6]
        if len(active) < max_voices:
            kept.append(n)
            continue
        ap = sorted(int(round(float(k.pitch))) for k in active)
        p = int(round(float(n.pitch)))
        if p > ap[-1] or p < ap[0]:        # 新音更极端（更高/更低）→ 挤掉最中间那个
            mid_val = ap[len(ap) // 2]
            for k in active:
                if int(round(float(k.pitch))) == mid_val:
                    kept.remove(k)
                    break
            kept.append(n)
        # 否则：既非最高也非最低的 filler，丢弃
    kept.sort(key=lambda n: float(n.start))
    return kept


def map_midi_to_events(
    midi_path,
    bpm: int = 120,
    auto_transpose: bool = True,
    transpose: Optional[int] = None,
    min_duration: float = 0.06,
    dedupe_gap: float = 0.06,
    mono: Optional[bool] = None,
    melody_mode: Optional[str] = None,
    fold_out_of_range: Optional[bool] = None,
    same_key_gap: float = 0.03,
    contour_black_keys: bool = True,
    layered_octave: bool = False,
    layer_mode: str = LAYER_BY_PITCH,
    layer_count: Optional[int] = None,
    instrument: Optional[str] = None,
    audio_path: Optional[str] = None,
    arrange: str = "none",
    polyphony: int = 0,
    bass_optimize: bool = False,
    piano_input_mode: str = "keyboard",
    piano_arpeggio_ms: float = 30.0,
) -> Tuple[List[NoteEvent], Dict]:
    """读取 MIDI 并映射为按键事件列表 + 统计信息。

    instrument：按哪件乐器映射（id 或中文名）。不传则沿用 `instruments.active()`
    里当前那件 —— 界面在 ② 页选乐器时会先切好，CLI 则默认风物之诗琴。
    layer_count：A 模式切几层；不传按当前乐器的排数（风物之诗琴 3、首席女高音 2）。
    audio_path：**仅人声/单声部乐器生效**。给了分离人声轨（vocals.wav）时，
    用其实测基频给旋律做最终仲裁（修八度错误、剔除非人声/毛刺、起点贴字头）；
    不传则只做纯 MIDI 的修复（碎片合并 + Viterbi 八度纠错）。风物之诗琴
    完全忽略这个参数，行为一个字节都不变。

    mono / melody_mode / fold_out_of_range 传 None 时**按乐器的预设取值**
    （`Instrument.preset()`）：拨弦类多声部乐器 = 老行为，人声单声部乐器 =
    只取主旋律、超音域先按八度挪进音域。显式传 True/False 仍然覆盖预设。
    `layered_octave` 的默认值保持老样子 False（分层开关由 ② 页那个勾选框控制，
    界面换乐器时会按预设勾/取消，见 desktop_app._apply_instrument_preset）。

    arrange：**声部整理方式**（新增功能；默认 "none" = 不启用，完全沿用
        上面的 layered_octave / layer_mode 老参数，老代码路径一个字节都不动）：
        · "track" = 独立分轨（推荐）：按 MIDI 音轨分组（只有一个音轨时自动
          改按音高），每组按轨内中位音高分一个不同八度带，各声部错到不同
          八度、互不糊在一起；
        · "pitch" = 按音高分三段（低/中/高各占一个八度）；
        · "raw" = 原样多声部（不整理，所有音原样映射；保留给旧预设兼容）；
        · "piano" = **钢琴曲适配**：按「旋律 / 低音 / 和声」三个声部角色
          改编（调性识别 → 整体移调到自然音阶 → 角色分排 → 和声减薄 →
          半音改写），详见 piano_tool/piano_arrange.py。钢琴谱优先用这个。
        显式选这几者时会接管（覆盖）layered_octave / layer_mode。
    polyphony：同时最多几个音（降糊用；默认 0 = 不限制，老行为）。
        只减音数、不改起音/时值（节奏和原曲完全一致），永远保留最高的
        旋律和最低的贝斯；track / pitch / piano 三种模式生效。
    bass_optimize：「低音优化」（默认 False = 老行为）—— 非旋律声部
        （低音贝斯/和弦垫/伴奏）避让旋律，只在旋律休息的间隙里保留，
        不与旋律同时响（见 optimize_low_voice）。人声/单声部乐器自动跳过。
        自动移调的估计仍用**过滤前**的音符，所以开不开这个功能，
        移调结果都一样。
    piano_input_mode：仅 arrange="piano" 生效。``keyboard`` 保留同时和弦；
        ``screen``（鼠标点击）把和弦拆成短促琶音 —— 单个鼠标按不了真和弦。
    piano_arpeggio_ms：鼠标模式每个和弦音之间的间隔（毫秒）。
    """
    import pretty_midi

    ak = active()
    if instrument:
        from .instruments import use

        ak = use(instrument)

    preset = ak.preset()
    if mono is None:
        mono = bool(preset["mono"])
    if melody_mode is None:
        melody_mode = str(preset["melody_mode"])
    if fold_out_of_range is None:
        fold_out_of_range = bool(preset["fold_out_of_range"])
    mono = bool(mono)
    # 「连续人声线」只对人声乐器有意义；多声部乐器选了也按 skyline 走
    if not ak.monophonic and melody_mode == "vocal":
        melody_mode = "high"

    # ---- 人声/单声部乐器专用前处理（风物之诗琴完全不经过这里）----
    # 转写噪声（八度错误、碎片、幽灵毛刺）对拨弦乐器只是难听，对「一次一个音」
    # 的人声是灾难；抽完主旋律后先修稳，再进移调/落位。详见 vocal_path 模块。
    voice_prep: Optional[Dict] = None

    def _prep_voice(group: List) -> List:
        nonlocal voice_prep
        if not (ak.monophonic and mono) or not group:
            return group
        from .vocal_path import prepare_voice_melody

        group, st = prepare_voice_melody(group, ak, audio_path=audio_path)
        if voice_prep is None:
            voice_prep = st
        else:   # 分层（按音轨）模式下有多组，把计数合并起来
            for k, v in st.items():
                if isinstance(v, dict):
                    tgt = voice_prep.setdefault(k, {})
                    for k2, v2 in v.items():
                        if isinstance(v2, (int, float)):
                            tgt[k2] = tgt.get(k2, 0) + v2
                elif isinstance(v, (int, float)):
                    voice_prep[k] = voice_prep.get(k, 0) + v
        return group

    pm = pretty_midi.PrettyMIDI(str(midi_path))
    raw = []
    for _ti, _inst in enumerate(pm.instruments):
        for _n in _inst.notes:
            # 记下音轨号/是否鼓轨，供「钢琴曲适配」做手部分离和过滤。
            # pretty_midi.Note 是普通对象，加属性不影响后面的老流程。
            try:
                _n.track = _ti
                _n.is_drum = bool(getattr(_inst, "is_drum", False))
            except Exception:  # noqa: BLE001
                pass
            raw.append(_n)

    # ---- 「钢琴曲适配」：按声部角色改编，走独立算法后直接返回 ----
    # 钢琴谱的瓶颈不是「怎么八度折叠」，而是「哪些音该留、哪些音该让」。
    # 详见 piano_tool/piano_arrange.py 顶部说明。
    if str(arrange or "").strip().lower() == "piano":
        from .piano_arrange import arrange_piano

        voices = int(polyphony or 0) or 3
        # 原琴谱习惯：和弦最多按 3 个键（4 个以上按不过来，也容易糊）。
        if ak.row_count >= 3:
            voices = min(voices, 3)
        # 鼓轨不参与钢琴改编（鼓点的 MIDI 音高映射到琴键只会变成噪声）
        piano_notes = [n for n in raw if not getattr(n, "is_drum", False)]
        arranged, pstats = arrange_piano(
            piano_notes,
            instrument=ak,
            max_voices=voices,
            input_mode=piano_input_mode,
            arpeggio_ms=piano_arpeggio_ms,
            auto_transpose=bool(auto_transpose),
            transpose=transpose,
            contour=bool(contour_black_keys),
            melody_only=bool(mono),
            fold_out_of_range=bool(fold_out_of_range),
        )
        events = [
            NoteEvent(
                time=float(a.start),
                note=int(a.pitch),
                key=ak.key_by_midi.get(int(a.pitch), "?"),
                action="press",
                duration=max(0.04, float(a.end - a.start)),
            )
            for a in arranged
            if ak.key_by_midi.get(int(a.pitch))
        ]
        events.sort(key=lambda e: e.time)
        mapped_lo = min((e.note for e in events), default=None)
        mapped_hi = max((e.note for e in events), default=None)
        stats = {
            "total": len(events),
            "orig_range": pstats.get("orig_range"),
            "mapped_range": ([mapped_lo, mapped_hi]
                             if events else None),
            "keys_used": len({e.key for e in events}),
            "white_span": [ak.lowest_midi, ak.highest_midi],
            "instrument": ak.id,
            "instrument_name": ak.name,
            "monophonic": False,
            "voice_prep": None,
            "layered_used": False,
            "mono_used": bool(mono),
            "raw_total": len(raw),
            "dropped": max(0, len(raw) - len(events)),
            "dropped_range": int(pstats.get("dropped_range", 0)),
            "dup_removed": int(pstats.get("collisions", 0)),
            "mono": bool(mono),
            "black_replaced": int(pstats.get("chromatic_snapped", 0)),
            "contour": bool(contour_black_keys),
            "contour_redirect": 0,
            "octave_folded": int(pstats.get("octave_folded", 0)),
            "white_kept": max(0, len(events) - int(
                pstats.get("chromatic_snapped", 0))),
            "transpose": int((pstats.get("key") or {}).get("shift", 0)),
            "octave_shift": 0,
            "white_hit_rate": (pstats.get("key") or {}).get("coverage"),
            "layered": False,
            "layer_mode": None,
            "layers": [],
            "bass_optimize": False,
            "bass": None,
            "piano": pstats,
        }
        return events, stats

    # ---- 分层独立八度：先切层，每层单独挑一个八度偏移 ----
    # 关掉这个开关（默认）时下面的 groups 只有一层、偏移恒为 0，
    # 走的是和以前一模一样的老流程。
    layered = bool(layered_octave)
    layer_mode = str(layer_mode or LAYER_BY_PITCH)
    if layered and layer_mode not in LAYER_MODES:
        layer_mode = LAYER_BY_PITCH

    # ---- 「声部整理方式」（新增功能）----
    # arrange="none"（默认）时下面整段不生效：layered / layer_mode 保持
    # 老参数原值，走老代码路径，行为一个字节都不变。
    # track=独立分轨（按音轨/单轨时按音高，推荐）/ pitch=按音高分三段 /
    # raw=原样多声部（不整理）；显式选这三者时接管分层开关与分层方式，
    # 并启用配套算法（降同时音数 + 分轨八度带）。
    arrange_mode = str(arrange or "none")
    if arrange_mode not in ARRANGE_MODES:
        arrange_mode = "none"
    if arrange_mode in ("track", "pitch", "raw"):
        layered = arrange_mode in ("track", "pitch")
        layer_mode = LAYER_BY_TRACK if arrange_mode == "track" else LAYER_BY_PITCH
    polyphony = int(polyphony or 0)

    # 人声：把靠近同一时刻的重复音交给 humanize_articulation 合并成长音，
    # 而不是在这里直接丢掉（丢掉会连它的时值一起没了，听感是「短了一截」）。
    dedupe = 0.0 if ak.monophonic else dedupe_gap

    # 「独立分轨」按音轨拆：单轨 MIDI（如 basic-pitch 转写）没有轨可拆。
    # **不要退回「按音高硬切三层」**（2026-11-01 修）：钢琴/人声谱的音高是
    # 连续变化的，按 47/49、69/71 这种边界切层会让相邻音换到不同八度，
    # 旋律在边界处突然跳一个八度（实测用户 song.mid 就是这样「听着怪」）。
    # 单轨的正确做法是**整体落位**：一层、零偏移，保留原曲音程。
    track_mode = bool(layered) and layer_mode == LAYER_BY_TRACK
    track_fallback = ""
    if track_mode and arrange_mode == "track" \
            and sum(1 for _i in pm.instruments if _i.notes) < 2:
        track_mode = False
        layered = False
        layer_mode = LAYER_BY_PITCH
        track_fallback = "single_track"
        logger.info("独立分轨：只有一条 MIDI 音轨，改为整体落位（不按音高硬切三层）")

    # 「低音优化」在**扁平的音符流**上做（丢/裁后，按音轨分层要靠 _orig
    # 把副本归回原音轨）；自动移调的估计用过滤前的音符（transpose_source），
    # 开不开这个功能移调结果都一样。
    transpose_source = None
    bass_stats: Optional[Dict] = None

    if track_mode and arrange_mode == "track":
        # ===== 新增：独立分轨（各音轨/声部分占一个八度）=====
        # 先按音轨分组并记下每个音归谁（owner），扁平化后统一降同时音数，
        # 再按 owner 拆回各轨；每条轨按轨内中位音高排序、分一个不同八度带
        # （低轨→底排、高轨→顶排），各轨错到不同八度、互不打架。
        raw_groups: List[List] = []
        owner: Dict[int, int] = {}
        for inst in pm.instruments:
            g = clean_notes(inst.notes, min_duration=min_duration,
                            dedupe_gap=dedupe)
            if mono:
                g = _prep_voice(pick_melody(g, mode=melody_mode))
            if g:
                for n in g:
                    owner[id(n)] = len(raw_groups)
                raw_groups.append(g)
        notes_all = [n for g in raw_groups for n in g]
        # 降同时发声音数（只减音数、不改起音/时值 → 节奏和原曲一致）
        if notes_all and polyphony > 0:
            before = len(notes_all)
            notes_all = _reduce_polyphony(notes_all, max_voices=polyphony)
            logger.info("降同时音数：%d → %d（每个时刻最多留 %d 个音）",
                        before, len(notes_all), polyphony)
        # 「低音优化」：在扁平流上避让/裁剪，再按 owner 拆回各轨
        if transpose_source is None:
            transpose_source = notes_all
        if bass_optimize and not ak.monophonic and notes_all:
            span = ak.highest_midi - ak.lowest_midi
            lift_lo = ak.lowest_midi + int(span * 0.45)
            lift_hi = ak.lowest_midi + int(span * 0.75)
            notes_all, bass_stats = optimize_low_voice(
                notes_all, lift_to=(lift_lo, lift_hi))
            logger.info("低音优化：%d → %d 音（旋律 %d · 避让丢弃 %d · 间隙保留 %d）",
                        bass_stats["in"], bass_stats["out"], bass_stats["melody"],
                        bass_stats["dropped_overlap"], bass_stats["kept_gap"])
        groups = []
        for gi in range(len(raw_groups)):
            g = [n for n in notes_all
                 if owner.get(id(getattr(n, "_orig", n)), -1) == gi]
            if g:
                groups.append(g)
        if not groups:
            groups = [notes_all]
        bands = [None] * len(groups)
        if len(groups) > 1:
            # 每条轨分一个**不同**的八度带：按轨内中位音高排序
            # （低轨→底排、高轨→顶排），各轨错到不同八度、互不打架。
            def _med(gi: int) -> int:
                ps = sorted(int(round(float(n.pitch))) for n in groups[gi])
                return ps[len(ps) // 2] if ps else 0
            order = sorted(range(len(groups)), key=_med)
            all_bands = octave_bands(len(groups))
            for rank, gi in enumerate(order):
                if rank < len(all_bands):
                    bands[gi] = all_bands[rank]
    elif track_mode:
        # B：每层 = 一个 MIDI 音轨，各自清理/取旋律后再独立算八度（老行为）
        groups = []
        for inst in pm.instruments:
            g = clean_notes(inst.notes, min_duration=min_duration,
                            dedupe_gap=dedupe)
            if mono:
                g = _prep_voice(pick_melody(g, mode=melody_mode))
            if g:
                groups.append(g)
        bands: List[Optional[List[int]]] = [None] * len(groups)
    else:
        notes_all = clean_notes(raw, min_duration=min_duration, dedupe_gap=dedupe)
        if mono:
            notes_all = _prep_voice(pick_melody(notes_all, mode=melody_mode))
        # 「独立分轨（单轨回退整体落位）」与「按音高三分」都降同时音数
        # （只减音数、不改节奏）。arrange=none / raw 不走这里，老行为不变。
        if arrange_mode in ("track", "pitch") \
                and notes_all and polyphony > 0:
            before = len(notes_all)
            notes_all = _reduce_polyphony(notes_all, max_voices=polyphony)
            logger.info("降同时音数：%d → %d（每个时刻最多留 %d 个音）",
                        before, len(notes_all), polyphony)
        # 「低音优化」：非旋律声部避让旋律，只在旋律休息的间隙保留
        if transpose_source is None:
            transpose_source = notes_all
        if bass_optimize and not ak.monophonic and notes_all:
            # 抬升区间取乐器音域的中高段（约 45%~75%），把旋律从底排抬到
            # 中高音区，避免「旋律音区低 → 听起来全在低音区」。只换八度，
            # 音级/调不变。
            span = ak.highest_midi - ak.lowest_midi
            lift_lo = ak.lowest_midi + int(span * 0.45)
            lift_hi = ak.lowest_midi + int(span * 0.75)
            notes_all, bass_stats = optimize_low_voice(
                notes_all, lift_to=(lift_lo, lift_hi))
            logger.info("低音优化：%d → %d 音（旋律 %d · 避让丢弃 %d · 间隙保留 %d）",
                        bass_stats["in"], bass_stats["out"], bass_stats["melody"],
                        bass_stats["dropped_overlap"], bass_stats["kept_gap"])
        if layered:
            # A：按音高切成几层（默认 = 当前乐器的排数：风物之诗琴 3、首席女高音 2）
            n_layers = int(layer_count) if layer_count else ak.row_count
            groups = split_notes_by_pitch(notes_all, n_layers)
            # 八度带按琴排切；层数比排数多时用 None 补齐，别让 zip 把层截掉
            bands = octave_bands(n_layers)
            bands = (bands + [None] * len(groups))[:len(groups)]
        else:
            groups = [notes_all]
            bands = [None]
    if layered:
        # arrange=none = 老版本 A/B，必须用旧公式原样还原；
        # arrange=track/pitch = 新功能，用「中位音进带」。
        layers = plan_layer_shifts(
            groups, bands, legacy=(str(arrange_mode) == "none"))
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

    # 移调：显式指定优先，否则自动搜索最「白键友好」的调（只管调，不管音区）。
    # 估计用 transpose_source（「低音优化」**过滤前**的全量音符；没经过滤时
    # 就是 notes 本身）：开不开低音优化，自动移调的结果都完全一样，
    # 旋律的调不会因为这个功能而发生变化。
    _tp_src = transpose_source if transpose_source is not None else notes
    shift, hit_rate = 0, None
    if transpose is not None:
        shift = int(transpose)
        hit_rate = _white_hit_rate(_tp_src, shift)
    elif auto_transpose:
        shift, hit_rate = estimate_transposition(_tp_src)
    if hit_rate is None:
        hit_rate = _white_hit_rate(_tp_src, shift)  # 不移调时也要给出命中率，便于对比

    # ---- 人声音区归位：只做 ±12，不动调 ----
    # 参考谱（181 音）90% 落在 C4~C5、中位 G4；所以如果整条旋律的中位音高跑出
    # 舒服音区，就整体挪一个/几个八度回来。**只改八度，音级和调完全不变**——
    # 这样既不会像半音移调那样改调，也不会出现「E5 变成 C5」这种双重改动。
    octave_shift = 0
    if ak.comfort_band and notes:
        lo_c, hi_c = int(ak.comfort_band[0]), int(ak.comfort_band[1])
        weighted = sorted(
            (int(round(n.pitch)) + shift, max(float(n.end - n.start), 0.05))
            for n in notes
        )
        half = sum(w for _, w in weighted) / 2.0
        acc = 0.0
        median = weighted[0][0]
        for p, w in weighted:
            acc += w
            if acc >= half:
                median = p
                break
        guard = 0
        while median + octave_shift < lo_c and guard < 6:
            octave_shift += 12
            guard += 1
        guard = 0
        while median + octave_shift > hi_c and guard < 6:
            octave_shift -= 12
            guard += 1
        # 归位后不能把整条旋律推出琴键范围
        if not (ak.lowest_midi <= median + octave_shift <= ak.highest_midi):
            octave_shift = 0
    shift += octave_shift

    # 先做音高映射，再去掉「映射到同一键且几乎同时」的重复点击
    # （不同音高被吸附到同一个白键时会出现，实际是同一个键连点两次，纯噪声）
    pending: List[Tuple[float, int, float]] = []
    dropped_range = 0
    for group, layer_k in layers:
        for n in group:
            orig = int(round(n.pitch)) + shift + layer_k
            in_range = ak.lowest_midi <= orig <= ak.highest_midi
            if not in_range and not fold_out_of_range and not ak.monophonic:
                # 本琴只有白键，超音域的多半是低音贝斯；折叠上来的话会突然
                # 出现在旋律音区、听起来很乱，因此提供「直接丢弃」的选项。
                #
                # 人声例外：旋律本身可能整体落在音域外（比如女声在 C5~C6），
                # 提前丢弃会把整首歌丢掉。人声一律先交给 snap_sequence_vocal
                # 按八度挪进来，实在挪不进的才在那边丢掉。
                dropped_range += 1
                continue
            pending.append((float(n.start), orig,
                            float(max(0.05, n.end - n.start))))

    contour_redirect = 0
    mapped: List[Tuple[float, int, int, bool, float]] = []

    if bool(ak.monophonic):
        # ---- 人声 / 单声部：整条旋律**一起**落位，不做逐音独立折叠 ----
        # 逐音折叠会在音域边界把旋律劈出八度大跳（听感「耳朵疼」），
        # snap_sequence_vocal 改成「保原曲音程」，落不下的音直接丢。
        pending.sort(key=lambda it: (it[0], it[1]))
        vocal_targets = snap_sequence_vocal(
            [i[0] for i in pending], [i[1] for i in pending],
            comfort_band=(list(ak.comfort_band) if ak.comfort_band else None),
        )
        for (t, orig, dur), target in zip(pending, vocal_targets):
            if target is None:
                dropped_range += 1      # 八度怎么挪都放不进两个八度 → 丢掉
                continue
            in_range = ak.lowest_midi <= orig <= ak.highest_midi
            if orig % 12 not in WHITE_PITCH_CLASSES:
                nearest, _ = snap_to_white(orig)
                if target != nearest:
                    contour_redirect += 1
            mapped.append((t, int(target), orig,
                           (orig % 12 not in WHITE_PITCH_CLASSES) or (target != orig), dur))
    else:
        # ---- 多声部（拨弦/键盘）：老流程 —— 走向感知黑键改编 + 逐音就近吸附 ----
        if contour_black_keys and pending:
            pending.sort(key=lambda it: (it[0], it[1]))
            targets = snap_sequence_contour(
                [p[0] for p in pending], [p[1] for p in pending],
                same_key_gap=same_key_gap,
            )
        else:
            targets = None

        for idx, (t, orig, dur) in enumerate(pending):
            in_range = ak.lowest_midi <= orig <= ak.highest_midi
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
        "mapped_range": None,          # 最终落进当前乐器音域的范围
        "keys_used": 0,                # 实际用到几个不同的键
        "white_span": [ak.lowest_midi, ak.highest_midi],
        "instrument": ak.id,
        "instrument_name": ak.name,
        "monophonic": bool(ak.monophonic),   # 人声/单声部预设是否生效
        "voice_prep": voice_prep,            # 人声前处理统计（非人声乐器为 None）
        "layered_used": layered,             # 实际有没有走分层（人声预设会关掉）
        "mono_used": bool(mono),             # 实际有没有只取主旋律
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
        "octave_shift": octave_shift,   # 其中有多少是「只换八度、不动调」的归位
        "white_hit_rate": None if hit_rate is None else round(hit_rate, 4),
        "layered": layered,                       # 是否开了「分层独立八度」
        "layer_mode": layer_mode if layered else None,
        "track_fallback": track_fallback,         # single_track = 单轨改整体落位
        "layers": layer_reports,                  # 每层：目标带 / 八度偏移 / 音符数
        "bass_optimize": bool(bass_stats is not None),   # 是否开了「低音优化」
        "bass": bass_stats,                       # 低音优化统计（旋律/避让丢/间隙留）
    }

    for t, target, orig, replaced, dur in filtered:
        stats["total"] += 1
        mapped_lo = target if mapped_lo is None else min(mapped_lo, target)
        mapped_hi = target if mapped_hi is None else max(mapped_hi, target)
        keys_used.add(target)
        # 注意：是否黑键只看音级(pitch class)，不能看是否落在琴键音域内，
        # 否则会把「超出音域被八度折叠」的音误算成黑键（两者性质完全不同：
        # 八度折叠保留音级，只是换八度；黑键吸附才会改变音高、破坏旋律）。
        if orig % 12 not in WHITE_PITCH_CLASSES:
            stats["black_replaced"] += 1
        if not (ak.lowest_midi <= orig <= ak.highest_midi):
            stats["octave_folded"] += 1
        if not replaced and orig in ak.white_midis:
            stats["white_kept"] += 1

        events.append(
            NoteEvent(
                time=t,
                note=target,
                key=ak.key_by_midi.get(target, "?"),
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

    # ---- 原琴风格：旋律抬到中/高排（只对「独立分轨」生效）----
    # 网上原琴谱的旋律在中排/高排、底排做低音；自动转写的情歌旋律常是
    # C4~B4，直接映射会全挤在底排。这里只把旋律整体抬八度（音级不动），
    # 低音/伴奏原地不动，听感才像原琴。详见 piano_tool/lyre_layout.py。
    if arrange_mode == "track" and not ak.monophonic and events:
        from .lyre_layout import lift_melody_to_middle

        events, lyre_stats = lift_melody_to_middle(events, ak)
        stats["lyre_layout"] = lyre_stats
        if lyre_stats.get("melody_shift"):
            stats["total"] = len(events)
            stats["dropped"] = max(
                0, int(stats.get("dropped", 0)) + int(lyre_stats.get("dropped", 0)))
            if events:
                stats["mapped_range"] = [
                    min(e.note for e in events), max(e.note for e in events)]
                stats["keys_used"] = len({e.key for e in events})
        events.sort(key=lambda e: e.time)

    # ---- 人声：把旋律收进「参考谱」那样的形态 ----
    # 参考谱（用户提供）的统计特征：音域 C4~E5、最大跳进 7 半音、平均 2.1 半音、
    # 没有超过五度的跳进、也没有和弦。自动映射出来的旋律通常更「野」，
    # 所以这里按乐器自带的舒服音区/最大跳进再收一次（只做八度平移，音级不变）。
    if ak.monophonic and (ak.comfort_band or ak.comfort_max_leap):
        events, soft = soften_melody_range(events)
        stats["auto_soften"] = soft
        events.sort(key=lambda e: e.time)

    # ---- 人声「唱法」：把时值写成这个乐器真实能唱出来的样子 ----
    # 拨弦乐器没有 min_hold_ms 这些参数 → humanize_articulation 原样返回，零改动。
    if ak.min_hold_ms:
        events, art = humanize_articulation(events)
        stats["articulation"] = art

    contour_note = f"，走向改编 {contour_redirect}" if contour_black_keys else ""
    preset_note = "，人声预设：只取主旋律 + 保轮廓落位" if ak.monophonic else ""
    bass_note = ""
    if bass_stats:
        bass_note = (f"，低音优化：旋律 {bass_stats['melody']} 音 · "
                     f"避让丢弃 {bass_stats['dropped_overlap']} · "
                     f"间隙保留 {bass_stats['kept_gap']}")
    logger.info(
        "映射完成（%s%s）：%d 音符（原始 %d，清理 %d）→ 音域 %s → 移调 %+d 半音 → 落到 %s"
        "（琴键 %d~%d，用了 %d 个键），白键命中 %.1f%%，黑键替换 %d%s，八度折叠 %d，白键保留 %d，超音域丢弃 %d%s",
        ak.name, preset_note, stats["total"], stats["raw_total"], stats["dropped"],
        fmt_range(orig_range), shift, fmt_range(stats["mapped_range"]),
        ak.lowest_midi, ak.highest_midi,
        stats["keys_used"], (hit_rate or 0) * 100, stats["black_replaced"],
        contour_note, stats["octave_folded"], stats["white_kept"],
        stats["dropped_range"], bass_note,
    )
    if voice_prep:
        _frag = voice_prep.get("fragments") or {}
        _dp = voice_prep.get("octave_spikes") or {}
        _au = voice_prep.get("audio") or {}
        _msg = (f"人声前处理：八度纠错 {_dp.get('repaired', 0)} 个音"
                f"，合并同音碎片 {_frag.get('joined', 0)}"
                f"，删幽灵毛刺 {_frag.get('ghosts', 0)}")
        if _au.get("applied"):
            _msg += (f"；人声轨校准（覆盖率 {float(_au.get('coverage', 0)) * 100:.0f}%）："
                     f"按基频修正八度 {_au.get('octave_fixed', 0)}，"
                     f"剔除非人声 {_au.get('dropped_outside', 0)}／"
                     f"毛刺 {_au.get('dropped_mismatch', 0)}，"
                     f"起点贴字头 {_au.get('onset_snapped', 0)}")
        elif _au.get("skipped_reason"):
            _msg += f"；{_au['skipped_reason']}"
        logger.info(_msg)
    return events, stats


def build_events_json(events: List[NoteEvent]) -> List[dict]:
    out = []
    for e in events:
        item = {
            "time": round(e.time, 3),
            "note": e.note,
            "key": e.key,
            "action": e.action,
            # 时值必须落地，否则"按住时值"播放拿不到长短
            "duration": None if e.duration is None else round(e.duration, 3),
        }
        # 人声匹配标过的字要留住，否则整理/柔化一次就丢了
        lyric = getattr(e, "lyric", None)
        if lyric:
            item["lyric"] = lyric
        out.append(item)
    return out


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
    parser.add_argument("--bass-optimize", action="store_true",
                        help="开启「低音优化」：非旋律声部（低音/和弦垫/伴奏）避让旋律，"
                             "只在旋律休息的间隙里保留，不与旋律同时响")
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
        bass_optimize=args.bass_optimize,
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
    bass = stats.get("bass")
    if bass:
        print(f"  低音优化：旋律 {bass['melody']} 音 · 避让丢弃 "
              f"{bass['dropped_overlap']} · 间隙保留 {bass['kept_gap']}")
    if args.notation:
        p = Path(args.notation)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(export_key_notation(events), encoding="utf-8")
        print(f"✓ 按键谱已导出：{p}")


if __name__ == "__main__":
    main()
