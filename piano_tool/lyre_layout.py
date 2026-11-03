"""原琴（风物之诗琴）风格优化 —— 旋律寄存器。

网上原琴谱的普遍弹法（NGA 入门指南 / B站谱例）：

- 旋律放**中间排**（A S D F G H J = C5~B5）或**高音排**
  （Q W E R T Y U = C6~B6）；
- **底排**（Z X C V B N M = C4~B4）留给低音/伴奏；
- 和弦一般 2~3 个键，最右边（最高）是旋律；按不过来时省掉中低音。

自动转写出来的歌，旋律音区常常和人声原调一样（C4~B4），直接映射后
所有音都挤在底排 —— 音高是对的，但听起来又低又闷，不像原琴。

本模块只做一件事：**把旋律整条抬到中/高排**（只换八度、不改音级、
不改节奏），低音/伴奏原地不动。这样旋律变亮，底排自然成为低音。
"""
from __future__ import annotations

import math
from typing import Dict, List, Tuple

# 旋律下四分位低于 C5(72) 就整体抬八度；目标是让至少 75% 的旋律音
# 落在中间排/高音排，而不是压着底排。
MIDDLE_LO = 72.0


def _melody_band(instrument) -> List[int]:
    """旋律可用的音区：风物之诗琴中排+顶排（72~95）；两排乐器用顶排。"""
    bands = instrument.octave_bands()
    if len(bands) >= 3:
        return [bands[1][0], bands[-1][1]]
    return list(bands[-1])


def _pitch_percentile(events, indices, q: float) -> float:
    """旋律音高的 q 分位（0~1）。q25 比中位更能反映「整条旋律是不是偏低」。"""
    vals = sorted(float(events[i].note) for i in indices)
    if not vals:
        return 0.0
    pos = max(0.0, min(1.0, q)) * (len(vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return vals[lo] * (1.0 - frac) + vals[hi] * frac


def _weighted_median_pitch(events, indices) -> float:
    values = sorted(
        (float(events[i].note), max(float(events[i].duration or 0.0), 0.05))
        for i in indices
    )
    if not values:
        return 0.0
    total = sum(w for _, w in values)
    acc = 0.0
    for p, w in values:
        acc += w
        if acc >= total / 2.0:
            return p
    return values[-1][0]


def lift_melody_to_middle(
    events,
    instrument,
    same_key_gap: float = 0.03,
) -> Tuple[List, Dict]:
    """把旋律整体抬到中/高排；返回 (事件列表, 统计)。

    - 只动旋律（天际线旋律链），低音/伴奏不动；
    - 只做 ±12 的整数倍平移，音级和调完全不变；
    - 旋律超出中/高排时按八度折回，不丢音；
    - 抬升后同键同刻冲突：旋律优先，伴奏让位。
    """
    st = {
        "melody_shift": 0,
        "before_median": None,
        "after_median": None,
        "before_q25": None,
        "after_q25": None,
        "melody_notes": 0,
        "dropped": 0,
        "target": MIDDLE_LO,
    }
    if not events:
        return list(events), st

    # 延迟导入，避免 midi_mapper ↔ lyre_layout 循环依赖
    from .midi_mapper import _NoteLike, _melody_chain_ids

    def _sort_key(i, e):
        return (float(e.time), -float(e.note))

    events = list(events)
    lk = [_NoteLike(e) for e in events]
    melody_ids = set(_melody_chain_ids(
        lk, cluster_window=0.05, max_drop_semitones=15.0))
    melody_idx = {i for i, obj in enumerate(lk) if id(obj) in melody_ids}
    st["melody_notes"] = len(melody_idx)
    if not melody_idx:
        return events, st

    before = _weighted_median_pitch(events, melody_idx)
    before_q25 = _pitch_percentile(events, melody_idx, 0.25)
    st["before_median"] = before
    st["before_q25"] = before_q25

    # 下四分位已经在 C5 以上 → 不动（不要为了“统一”把高旋律降下来）
    if before_q25 >= MIDDLE_LO:
        st["after_median"] = before
        st["after_q25"] = before_q25
        return events, st

    # 抬到下四分位进中排：q25 69 → +12=81；q25 60 → +12=72；q25 50 → +24=74
    shift = int(math.ceil((MIDDLE_LO - before_q25) / 12.0)) * 12
    band = _melody_band(instrument)
    st["melody_shift"] = shift

    for i in melody_idx:
        p = int(events[i].note) + shift
        while p > band[1]:
            p -= 12
        while p < band[0]:
            p += 12
        events[i].note = p
        events[i].key = instrument.key_by_midi.get(p, events[i].key)

    st["after_median"] = _weighted_median_pitch(events, melody_idx)
    st["after_q25"] = _pitch_percentile(events, melody_idx, 0.25)

    # ---- 抬升后同键冲突：旋律优先，伴奏让位；同键太近也不重复 ----
    priority = {i: (3 if i in melody_idx else 1) for i in range(len(events))}
    kept: List[Tuple[int, object]] = []
    last_by_key: Dict[str, Tuple[int, object]] = {}
    for i, e in sorted(enumerate(events), key=lambda it: _sort_key(*it)):
        key = str(e.key)
        prev = last_by_key.get(key)
        if prev is not None and float(e.time) - float(prev[1].time) < same_key_gap:
            if priority[i] > priority[prev[0]]:
                if prev in kept:
                    kept.remove(prev)
                st["dropped"] += 1
            else:
                st["dropped"] += 1
                continue
        last_by_key[key] = (i, e)
        kept.append((i, e))

    out = [e for _i, e in kept]
    out.sort(key=lambda e: float(e.time))
    return out, st
