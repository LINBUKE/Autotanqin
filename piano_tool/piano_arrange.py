"""②-b 钢琴曲适配（arrange="piano"）。

为什么单开一套
--------------
原来的 ``raw / track / pitch`` 都是「按音高堆叠」：把钢琴谱的低音、和弦、
旋律一起八度折叠进 21 个白键。结果必然是同键冲突、低音和旋律挤在同一音区、
和弦被拆散。钢琴谱真正的编配逻辑是 **声部角色**：

    旋律（右手主声部） / 低音（左手根音） / 和声（中间和弦音）

各自有音区，黑键要按调性整体处理，而不是逐音就近吸附。本模块就是按这个
逻辑做一次「自动改编」：

1. **调性识别**：按时长加权，找让最多音符落进同一个自然音阶的移调量；
2. **整体移调**：移到 C 大调 / A 小调，调内音全部变成白键；
3. **声部分离**：天际线旋律链 → 旋律；起音窗内最低的非旋律音 → 低音；
   其余 → 和声；
4. **角色分排**：旋律占中/高排、低音占底排、和声占中间排（按角色分，
   不是按音高三等分）；
5. **和声减薄**：每个起音簇按「和弦音优先、去八度重复、去半音冲突」
   保留到「同时最多几个音」；
6. **半音改写**：旋律按走向、和声按和弦音，就近换成白键；
7. **冲突处理**：同键同刻只留优先级高的；同键太近不重复触发；
8. **弹奏方式适配**：键盘模式保留同时和弦；鼠标模式把和弦拆成短促琶音。

这是「改编」不是「无损转写」：21 个白键装不下钢琴的 88 键，必然要取舍。
这里保证的是 **旋律优先、低音有根、和声不散、听得出原曲**。
"""
from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

from .instruments import active

logger = logging.getLogger("piano_tool.piano_arrange")

# 自然音阶（C 大调 / A 小调共用的白键音级）
WHITE_PCS = (0, 2, 4, 5, 7, 9, 11)
WHITE_SET = frozenset(WHITE_PCS)

ROLE_MELODY = "melody"
ROLE_BASS = "bass"
ROLE_HARMONY = "harmony"
_ROLE_PRIORITY = {ROLE_MELODY: 3, ROLE_BASS: 2, ROLE_HARMONY: 1}

# 常见三/四音和弦模板（根音为 0）。只用来判断「哪些音算和弦音」。
_CHORD_TEMPLATES: Tuple[Tuple[str, Tuple[int, ...]], ...] = (
    ("maj", (0, 4, 7)),
    ("min", (0, 3, 7)),
    ("dom7", (0, 4, 7, 10)),
    ("min7", (0, 3, 7, 10)),
    ("maj7", (0, 4, 7, 11)),
    ("dim", (0, 3, 6)),
    ("aug", (0, 4, 8)),
    ("sus2", (0, 2, 7)),
    ("sus4", (0, 5, 7)),
    ("dim7", (0, 3, 6, 9)),
)

MIN_DURATION = 0.04          # 转写毛刺阈值（钢琴的装饰音通常也比这长）
CLUSTER_WINDOW = 0.05        # 同一和弦的起音差（秒）
BASS_SPAN = 0.15             # 找低音时看多长一段起音（覆盖快速分解和弦）
SAME_KEY_GAP = 0.03          # 同一个键的最小重触发间隔（秒）
MAX_VOICES = 3               # 原琴谱习惯：和弦最多 3 个键（NGA 入门指南）


@dataclass
class ArrangedNote:
    """改编后的一个按键事件。pitch 已经是最终白键音高。"""

    start: float
    end: float
    pitch: int
    role: str
    orig_pitch: float


class _N:
    """输入音符的最小副本（pretty_midi.Note 的字段子集 + 声部角色）。"""

    __slots__ = ("start", "end", "pitch", "velocity", "track", "role")

    def __init__(self, start: float, end: float, pitch: float,
                 velocity: float = 64.0, track: int = 0,
                 role: str = ROLE_HARMONY):
        self.start = float(start)
        self.end = float(end)
        self.pitch = float(pitch)
        self.velocity = float(velocity)
        self.track = int(track)
        self.role = role


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _dur(n) -> float:
    return max(float(n.end) - float(n.start), 0.0)


def _weighted_median(values: Sequence[Tuple[float, float]]) -> float:
    """values = [(音高, 权重)]，返回加权中位数。"""
    if not values:
        return 0.0
    pairs = sorted(values)
    total = sum(w for _, w in pairs)
    if total <= 0:
        return float(pairs[len(pairs) // 2][0])
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2.0:
            return float(v)
    return float(pairs[-1][0])


def _percentile(values: Sequence[float], q: float) -> float:
    """简单分位数（0~1），不依赖 numpy。"""
    if not values:
        return 0.0
    vals = sorted(float(v) for v in values)
    if len(vals) == 1:
        return vals[0]
    pos = max(0.0, min(1.0, q)) * (len(vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return vals[lo] * (1.0 - frac) + vals[hi] * frac


def _clusters(notes: Sequence[_N], window: float = CLUSTER_WINDOW) -> List[List[_N]]:
    """按起音时间分簇（和弦/同时按下的音归为一簇）。"""
    out: List[List[_N]] = []
    cur: List[_N] = []
    base = 0.0
    for n in sorted(notes, key=lambda x: (x.start, -x.pitch)):
        if cur and n.start - base > window:
            out.append(cur)
            cur = []
        if not cur:
            base = n.start
        cur.append(n)
    if cur:
        out.append(cur)
    return out


def _clean(notes, min_duration: float = MIN_DURATION) -> List[_N]:
    """复制成内部音符，去掉过短毛刺。"""
    out: List[_N] = []
    for n in notes:
        d = float(getattr(n, "end", 0.0)) - float(getattr(n, "start", 0.0))
        if d < min_duration:
            continue
        out.append(_N(
            float(n.start), float(n.end), float(n.pitch),
            float(getattr(n, "velocity", 64) or 64),
            int(getattr(n, "track", 0) or 0),
        ))
    out.sort(key=lambda x: (x.start, -x.pitch))
    return out


# ---------------------------------------------------------------------------
# 1) 调性识别
# ---------------------------------------------------------------------------
def detect_key_shift(notes: Sequence[_N]) -> Dict[str, float]:
    """找「把最多时值落进同一个自然音阶」的移调量。

    返回 ``{"shift": 移调半音(-6~+6), "coverage": 白键覆盖率, "collection_shift": 0~11}``。

    注意：这里选的是 **自然音集合**（C 大调和 A 小调共用 7 个白键），
    所以不需要区分大小调光谱；大小调同集合，移调结果完全一样。
    """
    weights = [0.0] * 12
    for n in notes:
        # 用 sqrt(时值) 加权：避免一个很长的低音贝斯把调性带跑
        weights[int(round(n.pitch)) % 12] += max(_dur(n), 0.03) ** 0.5
    total = sum(weights) or 1.0

    best_shift = 0
    best_rate = -1.0
    best_collection = 0
    for s in range(12):
        # 原始音级 p 加 s 后落进白键 ⇔ p = (白键 - s) % 12
        hit = sum(weights[(pc - s) % 12] for pc in WHITE_PCS)
        rate = hit / total
        dist = abs(((s + 6) % 12) - 6)       # 移到 [-6, +6] 后的绝对距离
        best_dist = abs(((best_shift + 6) % 12) - 6)
        if rate > best_rate + 1e-9 or (
                abs(rate - best_rate) <= 1e-9 and dist < best_dist):
            best_shift, best_rate, best_collection = s, rate, s

    shift = best_shift if best_shift <= 6 else best_shift - 12
    return {
        "shift": int(shift),
        "coverage": float(round(best_rate, 4)),
        "collection_shift": int(best_collection),
    }


# ---------------------------------------------------------------------------
# 2) 和弦音判断
# ---------------------------------------------------------------------------
def _chord_pcs(cluster: Sequence[_N]) -> Set[int]:
    """从一簇音里推断和弦音级；推断不出就返回全部音级（不做过滤）。"""
    pcs = {int(round(n.pitch)) % 12 for n in cluster}
    if len(pcs) <= 1:
        return set(pcs)

    best_tones: Set[int] = set(pcs)
    best_score = -1e9
    best_matched = 0
    for root in range(12):
        for _name, tpl in _CHORD_TEMPLATES:
            tones = {(root + i) % 12 for i in tpl}
            matched = len(pcs & tones)
            extra = len(pcs - tones)
            score = matched * 3.0 - extra * 1.2 + (1.0 if root in pcs else 0.0)
            if score > best_score:
                best_score = score
                best_tones = tones
                best_matched = matched
    return best_tones if best_matched >= 2 else set(pcs)


# ---------------------------------------------------------------------------
# 3) 声部分离
# ---------------------------------------------------------------------------
def _melody_ids(notes: Sequence[_N]) -> Set[int]:
    """天际线旋律链（复用 midi_mapper 的实现，保证口径一致）。

    有多个 MIDI 音轨时先做一次「手部分离」：音轨中位音高最高的那条
    （通常是右手）拿来提旋律，避免把左手高音区的伴奏/分解和弦当成旋律。
    只有一条轨（basic-pitch 转写常见）时直接在全部音上提。
    """
    if not notes:
        return set()

    def _chain(sub: Sequence[_N]) -> Set[int]:
        from .midi_mapper import _melody_chain_ids

        return set(_melody_chain_ids(
            list(sub), cluster_window=CLUSTER_WINDOW,
            max_drop_semitones=15.0,
        ))

    # ---- 多轨：优先从「中位音高最高」的那条轨提旋律 ----
    groups: Dict[int, List[_N]] = {}
    for n in notes:
        groups.setdefault(int(getattr(n, "track", 0) or 0), []).append(n)
    if len(groups) >= 2:
        meds = {
            tr: _weighted_median([(x.pitch, max(_dur(x), 0.05)) for x in g])
            for tr, g in groups.items()
        }
        top_track = max(meds, key=meds.get)
        top_group = groups[top_track]
        if (len(top_group) >= 3
                and meds[top_track] - min(meds.values()) >= 5.0):
            sub = sorted(top_group, key=lambda x: (x.start, -x.pitch))
            try:
                ids = _chain(sub)
                # 提出来的链太少（这条轨其实全是伴奏）→ 退回全谱天际线
                if len(ids) >= max(3, int(len(sub) * 0.25)):
                    return ids
            except Exception:  # noqa: BLE001
                pass

    try:
        return _chain(sorted(notes, key=lambda x: (x.start, -x.pitch)))
    except Exception:  # noqa: BLE001  极端情况下退回「每簇最高音」
        logger.warning("旋律链提取失败，退回每簇最高音", exc_info=True)
        ids: Set[int] = set()
        for cluster in _clusters(notes):
            top = max(cluster, key=lambda n: (n.pitch, n.velocity))
            ids.add(id(top))
        return ids


def _fix_melody_octaves(notes: Sequence[_N], melody_ids: Set[int]) -> int:
    """修转写旋律里的「孤立八度错误」。

    basic_pitch 这类转写经常把某个音判高/低一个八度；在编辑器里听单个音
    没问题，但上琴后旋律会突然跳八度。规则：如果前后两个旋律音彼此接近
    （≤5 半音），而当前音与前后都差 ≥10 半音，就把当前音折回最近的八度。
    只处理孤立点，不碰真正的连续八度跳进。
    """
    seq = sorted((n for n in notes if id(n) in melody_ids),
                 key=lambda n: (n.start, -n.pitch))
    fixed = 0
    for i in range(1, len(seq) - 1):
        p = float(seq[i].pitch)
        prev = float(seq[i - 1].pitch)
        nxt = float(seq[i + 1].pitch)
        if abs(prev - nxt) > 5.0:
            continue
        if abs(p - prev) < 10.0 or abs(p - nxt) < 10.0:
            continue
        mid = (prev + nxt) / 2.0
        cand = p - 12.0 if p > mid else p + 12.0
        if abs(cand - mid) < abs(p - mid) - 1e-9:
            seq[i].pitch = cand
            fixed += 1
    return fixed


def _assign_bass(notes: Sequence[_N], melody_ids: Set[int],
                 clusters: Sequence[Sequence[_N]], bass_span: float = BASS_SPAN) -> Set[int]:
    """给每个「起音窗」分配一个低音：窗内最低、且不是旋律的那个音。

    为什么要看一段窗而不是单个起音簇：分解和弦（左手 do-mi-so）每个音
    单独起音，如果每簇都取最低，就会把整串琶音都判成低音、全折进底排。
    看 150ms 窗口后，整串琶音只有最低那个是低音，其余留给和声。
    """
    if not notes:
        return set()
    starts = [n.start for n in notes]
    bass_ids: Set[int] = set()
    assigned_bass: Set[int] = set()

    for cluster in clusters:
        t0 = cluster[0].start
        lo = bisect.bisect_left(starts, t0 - 1e-9)
        hi = bisect.bisect_right(starts, t0 + bass_span)
        pool = [n for n in notes[lo:hi] if n.start >= t0 - 1e-9]
        if not pool:
            continue
        lowest = min(pool, key=lambda n: (n.pitch, n.start))
        # 最低音是旋律 → 这一窗没有独立低音（旋律本身就在低音区）
        if id(lowest) in melody_ids:
            continue
        # 最低音已经被上一窗判成低音 → 这一窗不再制造第二个低音
        if id(lowest) in assigned_bass:
            continue
        bass_ids.add(id(lowest))
        assigned_bass.add(id(lowest))
    return bass_ids


# ---------------------------------------------------------------------------
# 4) 和声减薄
# ---------------------------------------------------------------------------
def _harmony_score(n: _N, chord_pcs: Set[int], anchors: Sequence[_N]) -> float:
    """一个和声音的保留价值：和弦音 > 非和弦音；不重复锚点音级。"""
    pc = int(round(n.pitch)) % 12
    score = 3.0 if pc in chord_pcs else 0.0
    score += min(_dur(n), 1.0) * 0.5
    if pc in {int(round(a.pitch)) % 12 for a in anchors}:
        score -= 2.0          # 与旋律/低音同音级（八度重复）→ 价值低
    return score


def _reduce_roles(
    notes: Sequence[_N],
    melody_ids: Set[int],
    bass_ids: Set[int],
    max_voices: int,
    cluster_window: float = CLUSTER_WINDOW,
) -> Tuple[List[_N], Dict[str, int]]:
    """每个起音簇按角色保留到 max_voices；返回保留音和统计。"""
    stats = {"harmony_dropped": 0, "melody_dropped": 0}
    kept: List[_N] = []

    for cluster in _clusters(notes, cluster_window):
        mel = sorted([n for n in cluster if id(n) in melody_ids],
                     key=lambda x: (-x.pitch, -_dur(x), -x.velocity))
        bass = [n for n in cluster if id(n) in bass_ids]
        # 原琴只有 3 个键可同时按：旋律只留最高音 1 个；同一簇里的
        # 其余旋律音（双音/八度重复）不直接丢，降级成和声候选，
        # 有预算就留一个，没有才丢。
        harm = [n for n in cluster
                if id(n) not in melody_ids and id(n) not in bass_ids]
        harm += mel[1:]

        mel_kept: List[_N] = []
        used_pcs: Set[int] = set()
        if mel:
            top = mel[0]
            pc = int(round(top.pitch)) % 12
            mel_kept.append(top)
            used_pcs.add(pc)

        # 低音：每个簇最多 1 个（正常情况下 _assign_bass 已经保证）
        bass_kept = sorted(bass, key=lambda x: x.pitch)[:1]
        if bass:
            stats["harmony_dropped"] += max(0, len(bass) - len(bass_kept))
        for n in bass_kept:
            used_pcs.add(int(round(n.pitch)) % 12)

        # 没有旋律也没有低音的簇（纯和声）：把最高音当旋律，保证线条不断
        if not mel_kept and not bass_kept and harm:
            top = max(harm, key=lambda x: (x.pitch, _dur(x)))
            mel_kept = [top]
            harm = [n for n in harm if id(n) != id(top)]

        budget = max(0, max_voices - len(mel_kept) - len(bass_kept))
        chord_pcs = _chord_pcs(cluster)
        ranked = sorted(
            harm,
            key=lambda n: (-_harmony_score(n, chord_pcs, mel_kept + bass_kept),
                           -_dur(n), -n.velocity, n.pitch),
        )
        harm_kept: List[_N] = []
        for n in ranked:
            if len(harm_kept) >= budget:
                stats["harmony_dropped"] += 1
                continue
            pc = int(round(n.pitch)) % 12
            if pc in used_pcs:          # 去八度/同音级重复
                stats["harmony_dropped"] += 1
                continue
            # 去半音冲突：21 键是纯白键、没有力度变化，小二度叠在一起
            # 只会听成「打架」。宁可少一个音，也不留半音对。
            if any((pc - u) % 12 in (1, 11) for u in used_pcs):
                stats["harmony_dropped"] += 1
                continue
            harm_kept.append(n)
            used_pcs.add(pc)

        kept.extend(mel_kept + bass_kept + harm_kept)

    return kept, stats


def _cap_voices(notes: Sequence[_N], max_voices: int) -> Tuple[List[_N], int]:
    """限制同一时刻的发声数；只牺牲和声，旋律和低音永远保留。

    和声减薄是按「起音簇」算的，长音和下一簇重叠时总数仍可能超标。
    这里做一次时间扫：新音进来时若已满，和声直接丢；旋律/低音进来时
    挤掉最近的一个和声。
    """
    out: List[_N] = []
    active: List[_N] = []
    dropped = 0
    for n in sorted(notes, key=lambda x: (x.start, -_ROLE_PRIORITY[_role_of(x)],
                                          -x.pitch)):
        active = [a for a in active if a.end > n.start + 1e-9]
        if len(active) >= max_voices:
            if _role_of(n) == ROLE_HARMONY:
                dropped += 1
                continue
            victim = None
            for a in reversed(active):
                if _role_of(a) == ROLE_HARMONY:
                    victim = a
                    break
            if victim is not None:
                if victim in out:
                    out.remove(victim)
                active.remove(victim)
                dropped += 1
        out.append(n)
        active.append(n)
    return out, dropped


def _role_of(n: _N) -> str:
    return getattr(n, "role", ROLE_HARMONY)


def _set_role(n: _N, role: str) -> None:
    n.role = role


# ---------------------------------------------------------------------------
# 5) 半音改写
# ---------------------------------------------------------------------------
def _directions(notes: Sequence[_N], ids: Set[int]) -> Dict[int, int]:
    """给旋律/低音序列逐音算「走向」：上行 +1、下行 -1、判断不了 0。"""
    seq = sorted([n for n in notes if id(n) in ids], key=lambda x: x.start)
    dirs: Dict[int, int] = {}
    for i, n in enumerate(seq):
        prev = seq[i - 1].pitch if i > 0 else None
        nxt = seq[i + 1].pitch if i + 1 < len(seq) else None
        if prev is not None and nxt is not None:
            d = 1 if nxt > prev else (-1 if nxt < prev else 0)
        elif nxt is not None:
            d = 1 if nxt > n.pitch else -1
        elif prev is not None:
            d = 1 if n.pitch > prev else -1
        else:
            d = 0
        dirs[id(n)] = d
    return dirs


def _snap_pitch(pitch: float, direction: int, chord_pcs: Set[int]) -> Tuple[int, bool]:
    """把一个音换到最近的白键；方向、和弦音优先。返回 (新音高, 是否改写)。"""
    p = int(round(pitch))
    if p % 12 in WHITE_SET:
        return p, False

    best_p = p
    best_score = -1e9
    for delta in (-2, -1, 1, 2):
        cand = p + delta
        if cand % 12 not in WHITE_SET:
            continue
        score = -abs(delta) * 2.0
        if direction > 0 and delta > 0:
            score += 0.8
        elif direction < 0 and delta < 0:
            score += 0.8
        if cand % 12 in chord_pcs:
            score += 1.5
        if score > best_score or (
                abs(score - best_score) <= 1e-9
                and abs(cand - p) < abs(best_p - p)):
            best_p, best_score = cand, score
    return best_p, True


def _diatonicize(
    notes: Sequence[_N],
    melody_ids: Set[int],
    bass_ids: Set[int],
    clusters: Sequence[Sequence[_N]],
) -> Tuple[List[int], Set[int]]:
    """把所有黑键音换成白键音；返回改写后的音高列表和「改写过的对象 id」。"""
    chord_by_note: Dict[int, Set[int]] = {}
    for cluster in clusters:
        cps = _chord_pcs(cluster)
        for n in cluster:
            chord_by_note[id(n)] = cps

    dirs = _directions(notes, melody_ids)
    dirs.update(_directions(notes, bass_ids))

    new_pitches: List[int] = []
    changed: Set[int] = set()
    for n in notes:
        direction = dirs.get(id(n), 0) if (
            id(n) in melody_ids or id(n) in bass_ids) else 0
        p, changed_flag = _snap_pitch(
            n.pitch, direction, chord_by_note.get(id(n), set()))
        if changed_flag:
            changed.add(id(n))
        new_pitches.append(p)
    return new_pitches, changed


# ---------------------------------------------------------------------------
# 6) 角色分排（旋律中/高排、低音底排、和声中间排）
# ---------------------------------------------------------------------------
def _bands(instrument) -> Tuple[List[int], List[int], List[int]]:
    """返回 (低音带, 和声带, 旋律带)。旋律带可以跨两排（保持旋律音域）。"""
    bands = instrument.octave_bands()
    bass = bands[0]
    harmony = bands[1] if len(bands) > 1 else bands[0]
    if len(bands) >= 3:
        melody = [bands[1][0], bands[-1][1]]      # 中排 + 顶排 = 两个八度
    else:
        melody = list(bands[-1])
    return bass, harmony, melody


def _fold_into_band(p: int, lo: int, hi: int) -> int:
    """八度折叠进某一段（只换八度、不改音级）。"""
    while p < lo:
        p += 12
    while p > hi:
        p -= 12
    return p


def _place_roles(
    notes: Sequence[_N],
    pitches: Sequence[int],
    instrument,
    fold_out_of_range: bool = True,
) -> Tuple[List[Optional[int]], int, int, Dict[str, int]]:
    """按角色做**整体**八度平移，再把每个角色折进自己的排。

    2026-11-02 修：以前会把每个音硬塞进该角色的窄八度带（低音 1 个八度、
    和声 1 个八度），实测一首 4 分钟的谱有 **44% 的音被单独折八度** ——
    同一句旋律/和弦忽高忽低，听感就是「一点规律都没有」。现在先按角色的
    中位音高整体平移（保持句内音程），再做**角色内的八度归位**：

    - 低音 → 底排（C4~B4）
    - 和声 → 中排（C5~B5，14 键乐器则顶排）
    - 旋律 → 中排+顶排（保留两个八度）

    这种折叠不是「随机逐音折八度」：低音/和声本来就是伴奏，八度归位后
    始终在正确排；旋律只在转写八度错误时才会折。返回
    (每个音的最终音高或 None=丢弃, 通用折叠数, 丢弃数, 各角色归位数)。
    """
    bass_band, harmony_band, melody_band = _bands(instrument)

    centers = {
        ROLE_BASS: (bass_band[0] + bass_band[1]) / 2.0,
        ROLE_HARMONY: (harmony_band[0] + harmony_band[1]) / 2.0,
        ROLE_MELODY: melody_band[0] + (melody_band[1] - melody_band[0]) * 0.55,
    }

    # 先按角色算整体八度偏移（保持旋律/低音轮廓，不逐音硬折）
    shifts: Dict[str, int] = {}
    for role in (ROLE_MELODY, ROLE_BASS, ROLE_HARMONY):
        vals = [(float(p), max(_dur(n), 0.05))
                for n, p in zip(notes, pitches) if _role_of(n) == role]
        if not vals:
            shifts[role] = 0
            continue
        med = _weighted_median(vals)
        k = int(round((centers[role] - med) / 12.0)) * 12
        shifts[role] = max(-48, min(48, k))

    final: List[Optional[int]] = []
    folded = 0
    dropped = 0
    role_folded = {ROLE_MELODY: 0, ROLE_BASS: 0, ROLE_HARMONY: 0}
    lo, hi = instrument.lowest_midi, instrument.highest_midi
    for n, p in zip(notes, pitches):
        role = _role_of(n)
        q = int(round(p)) + shifts.get(role, 0)
        # 先处理「超出乐器音域」：drop 模式直接丢，不做任何八度折叠
        # （保留 2026-11-02 的原则：宁可少一个音，也不要逐音乱折）。
        if not (lo <= q <= hi):
            if fold_out_of_range:
                q = _fold_into_band(q, lo, hi)
                folded += 1
            else:
                final.append(None)
                dropped += 1
                continue
        # 音域内的音再做角色内八度归位：低音到底排、和声到中排、
        # 旋律到中/顶排。这样每个角色都落在正确的排上。
        if role == ROLE_BASS:
            band = bass_band
        elif role == ROLE_HARMONY:
            band = harmony_band
        else:
            band = melody_band
        q_fold = _fold_into_band(q, band[0], band[1])
        if q_fold != q:
            role_folded[role] += 1
            q = q_fold
        final.append(q)
    return final, folded, dropped, role_folded


# ---------------------------------------------------------------------------
# 7) 冲突 / 重触发
# ---------------------------------------------------------------------------
def _resolve_collisions(
    notes: Sequence[_N],
    pitches: Sequence[int],
    instrument,
    same_key_gap: float = SAME_KEY_GAP,
) -> Tuple[List[Tuple[_N, int]], int]:
    """同一键同一刻/太近只留一个；返回 (笔记, 音高) 列表和丢弃数。"""
    items = sorted(
        zip(notes, pitches),
        key=lambda it: (it[0].start, -_ROLE_PRIORITY[_role_of(it[0])], -it[1]),
    )
    out: List[Tuple[_N, int]] = []
    last_by_key: Dict[str, Tuple[_N, int]] = {}
    dropped = 0

    for n, p in items:
        key = instrument.key_by_midi.get(int(p))
        if key is None:
            dropped += 1
            continue
        prev = last_by_key.get(key)
        if prev is not None and n.start - prev[0].start < same_key_gap:
            # 高优先级（旋律 > 低音 > 和声）保留；同优先级保留先到的
            if _ROLE_PRIORITY[_role_of(n)] > _ROLE_PRIORITY[_role_of(prev[0])]:
                if prev in out:
                    out.remove(prev)
                dropped += 1
            else:
                dropped += 1
                continue
        last_by_key[key] = (n, p)
        out.append((n, p))

    # 同一个键前后重叠：把前一个的尾巴裁到后一个起音前，避免「按住时值」卡键
    out.sort(key=lambda it: it[0].start)
    last_by_key = {}
    for n, p in out:
        key = instrument.key_by_midi.get(int(p))
        prev = last_by_key.get(key) if key else None
        if prev is not None:
            pn = prev[0]
            if pn.end > n.start:
                pn.end = max(pn.start + 0.04, n.start - 0.004)
        if key:
            last_by_key[key] = (n, p)
    return out, dropped


# ---------------------------------------------------------------------------
# 8) 鼠标模式：和弦拆成短促琶音
# ---------------------------------------------------------------------------
def _spread_chords(notes: Sequence[_N], gap_ms: float) -> None:
    """把同一时刻的和弦拆成琶音（原地改 start/end）。"""
    gap = max(0.0, gap_ms / 1000.0)
    if gap <= 0:
        return
    groups: Dict[int, List[_N]] = {}
    for n in notes:
        bucket = int(round(n.start * 1000.0))
        groups.setdefault(bucket, []).append(n)
    for group in groups.values():
        if len(group) < 2:
            continue
        ordered = sorted(
            group,
            key=lambda n: (-_ROLE_PRIORITY[_role_of(n)], -n.pitch),
        )
        t0 = min(n.start for n in ordered)
        for i, n in enumerate(ordered):
            n.start = t0 + i * gap
            if i + 1 < len(ordered):
                n.end = min(n.end, n.start + gap * 0.9)
            else:
                n.end = max(n.start + 0.04, n.end)


def _serialize(notes: Sequence[_N]) -> None:
    """鼠标只能按一个键：把每个音的尾巴裁到下一个起音前（原地改 end）。"""
    ordered = sorted(notes, key=lambda n: (n.start, -_ROLE_PRIORITY[_role_of(n)]))
    for i, n in enumerate(ordered):
        if i + 1 < len(ordered):
            nxt = ordered[i + 1].start
            if n.start + 0.04 > nxt:
                continue
            if n.end > nxt:
                n.end = max(n.start + 0.04, nxt - 0.004)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def arrange_piano(
    notes,
    instrument=None,
    max_voices: int = MAX_VOICES,
    input_mode: str = "keyboard",
    arpeggio_ms: float = 30.0,
    auto_transpose: bool = True,
    transpose: Optional[int] = None,
    contour: bool = True,
    melody_only: bool = False,
    fold_out_of_range: bool = True,
) -> Tuple[List[ArrangedNote], Dict]:
    """把完整钢琴谱改编成当前乐器的白键琴谱。

    参数
    ----
    notes：pretty_midi.Note 或任何有 start/end/pitch/velocity 的对象。
    max_voices：同时最多几个音（含旋律和低音）。「尽量保留和声厚度」建议 5~6。
    input_mode：``keyboard`` 保留同时和弦；``screen``（鼠标）拆成琶音。
    arpeggio_ms：鼠标模式每个和弦音之间的间隔（毫秒）。
    auto_transpose：是否自动识别调性并整体移调；False 时按原调（只改黑键）。
    transpose：显式移调半音数，优先于 auto_transpose。
    contour：黑键是否按旋律走向改写（False = 就近换白键）。
    melody_only：只输出主旋律（对应 ② 页「只保留主旋律」）。
    fold_out_of_range：超出 21 键音域时 True=八度折回、False=直接丢弃。
        默认 True（CLI/老调用）；桌面版按 ② 页「超音域音」设置传，默认 drop，
        避免逐音折八度把旋律/和弦的规律打乱。
    """
    ak = instrument or active()
    max_voices = max(1, min(8, int(max_voices or MAX_VOICES)))

    src = _clean(notes)
    if not src:
        return [], {
            "total": 0, "orig_range": None, "mapped_range": None,
            "keys_used": 0, "roles": {ROLE_MELODY: 0, ROLE_BASS: 0,
                                     ROLE_HARMONY: 0},
            "key": {"shift": 0, "coverage": 0.0},
        }

    # ---- 调性识别 / 整体移调 ----
    if transpose is not None:
        shift = int(transpose)
        key_info = {"shift": shift, "coverage": None}
    elif auto_transpose:
        key_info = detect_key_shift(src)
        shift = int(key_info["shift"])
    else:
        shift = 0
        key_info = {"shift": 0, "coverage": None}

    orig_pitches = [int(round(n.pitch)) for n in src]   # 移调前，给界面看
    for n in src:
        n.pitch = float(int(round(n.pitch)) + shift)

    clusters = _clusters(src)
    melody_ids = _melody_ids(src)
    melody_octave_fixed = _fix_melody_octaves(src, melody_ids)
    bass_ids = _assign_bass(src, melody_ids, clusters)

    # 给每个音挂上角色（后面所有步骤都按角色处理）
    for cluster in clusters:
        for n in cluster:
            if id(n) in melody_ids:
                _set_role(n, ROLE_MELODY)
            elif id(n) in bass_ids:
                _set_role(n, ROLE_BASS)
            else:
                _set_role(n, ROLE_HARMONY)

    if melody_only:
        kept = [n for n in src if id(n) in melody_ids]
        for n in kept:
            _set_role(n, ROLE_MELODY)
    else:
        kept, reduce_stats = _reduce_roles(
            src, melody_ids, bass_ids, max_voices)

    cap_dropped = 0
    # 不再做「按 active 长音数」的二次降糊：原琴是拨弦，长音不会真的占住
    # 复音位；同一时刻的按键数已由 _reduce_roles 按起音簇限制到 max_voices。
    # 之前这一步会把「旋律+低音的长音」算成已占满 3 个位置，导致大量和声被丢，
    # 输出几乎只剩旋律+低音（实测 1268 个音里只有 57 个和声）。

    # ---- 半音改写（在分排前做：只改音级，不动八度）----
    # 注意：keep 里可能少了和声，和弦判断用原始簇更完整，所以聚类仍用 src。
    if contour:
        pitches, changed = _diatonicize(kept, melody_ids, bass_ids, clusters)
    else:
        pitches = [int(round(n.pitch)) for n in kept]
        changed = set()
        for i, p in enumerate(pitches):
            if p % 12 not in WHITE_SET:
                pitches[i], _ = _snap_pitch(float(p), 0, set())
                changed.add(id(kept[i]))

    # ---- 角色分排 ----
    placed, octave_folded, dropped_range, role_folded = _place_roles(
        kept, pitches, ak, fold_out_of_range=bool(fold_out_of_range))
    kept = [n for n, p in zip(kept, placed) if p is not None]
    pitches = [int(p) for p in placed if p is not None]

    # 如果某件乐器音域对不上（例如人声 14 键），再兜一次底
    for i, p in enumerate(pitches):
        pitches[i] = _fold_into_band(
            int(p), ak.lowest_midi, ak.highest_midi)

    # ---- 冲突 / 重触发 ----
    items, collisions = _resolve_collisions(kept, pitches, ak)

    # ---- 鼠标模式：和弦拆成琶音 + 全局串行 ----
    final_notes: List[_N] = [n for n, _ in items]
    if str(input_mode) == "screen":
        _spread_chords(final_notes, arpeggio_ms)
        _serialize(final_notes)

    # 排序：同一刻旋律优先（鼠标点击时先落旋律）
    final_items = sorted(
        zip(final_notes, [p for _n, p in items]),
        key=lambda it: (it[0].start, -_ROLE_PRIORITY[_role_of(it[0])],
                        -it[1]),
    )

    arranged: List[ArrangedNote] = []
    role_counts = {ROLE_MELODY: 0, ROLE_BASS: 0, ROLE_HARMONY: 0}
    used_keys: Set[str] = set()
    for n, p in final_items:
        role = _role_of(n)
        role_counts[role] = role_counts.get(role, 0) + 1
        key = ak.key_by_midi.get(int(p))
        if key:
            used_keys.add(key)
        arranged.append(ArrangedNote(
            start=float(n.start),
            end=float(n.end),
            pitch=int(p),
            role=role,
            orig_pitch=float(n.pitch),   # 已含移调，便于回溯
        ))

    mapped_lo = min((a.pitch for a in arranged), default=None)
    mapped_hi = max((a.pitch for a in arranged), default=None)
    stats = {
        "total": len(arranged),
        "orig_range": [min(orig_pitches), max(orig_pitches)] if orig_pitches else None,
        "mapped_range": [mapped_lo, mapped_hi] if arranged else None,
        "keys_used": len(used_keys),
        "roles": role_counts,
        "key": key_info,
        "coverage": key_info.get("coverage"),
        "chromatic_snapped": len(changed),
        "octave_folded": octave_folded,
        "role_folded": role_folded,
        "melody_octave_fixed": melody_octave_fixed,
        "dropped_range": dropped_range,
        "collisions": collisions,
        "harmony_dropped": (
            reduce_stats.get("harmony_dropped", 0) if not melody_only else 0),
        "melody_dropped": (
            reduce_stats.get("melody_dropped", 0) if not melody_only else 0),
        "voice_dropped": cap_dropped,
        "max_voices": max_voices,
        "input_mode": str(input_mode),
        "arpeggio_ms": float(arpeggio_ms),
        "melody_only": bool(melody_only),
    }
    return arranged, stats
