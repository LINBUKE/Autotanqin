"""首席女高音（人声）专用前处理：让转写结果「稳」地落到 14 键上。

为什么单独一个模块
------------------
人声乐器（单声部、两个八度、一次只能唱一个音）和拨弦乐器是两种东西，
处理方式根本不同。本模块**只被人声/单声部分支调用**，风物之诗琴路径
一概不经过这里，行为一个字节都不会变。

实测过的问题（2026-10-21，郑浩 vocals + song.mid 644 音琴谱）
------------------------------------------------------------
basic_pitch 对人声轨的转写有两大类「不稳」的噪声（用音频实测基频仲裁过）：
  · **八度混乱**：原始旋律 656 音里有 76 处相邻八度跳，音频仲裁确认
    **75 处是转写错**（基频平直，MIDI 自己跳八度），只有 1 处是真跳。
    落位算法「保原曲音程」会忠实继承这些错误 → 弹出来就是八度 zigzag。
  · **毛刺/碎片**：80 个与前后邻居都大跳的短音里 41 个是假音
    （基频与邻居一致）；同音被切成两段、黑白键之间插幽灵音也常见。

走过的弯路（记下来，别再走）
--------------------------
  试过全局 Viterbi 修八度（带/不带「主场音区」先验都试了）：**失败**。
  这个转写的八度错误是**双向**的，旋律中位音高离真相只有 2.87 半音
  （大部分音本来就对），而任何全局平滑都会在「歧义乐句」上投票 ——
  实测把中位偏差从 2.87 恶化到 7~8.8。结论：**没有音频证据时，只修
  局部证据确凿的（跳出去又跳回来的 zigzag），绝不全局重排**。

本模块做三件事（都只改「哪些音 / 哪个八度 / 什么时候响」，
**不改音级、不生成新音高、不动播放链路**）：
  1. `merge_voice_fragments`  同音碎片合并、幽灵毛刺删除
  2. `repair_octave_spikes`   局部 zigzag 修复（不用音频也能修，保守）
  3. `correct_with_audio`     有人声轨时用实测基频做最终仲裁（主力，
                               桌面版在「先人声分离再转写」时会自动带上）
"""
from __future__ import annotations

import bisect
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("piano_tool.vocal_path")

__all__ = [
    "correct_with_audio",
    "merge_voice_fragments",
    "prepare_voice_melody",
    "repair_octave_spikes",
]


def _copy_note(n, pitch: Optional[int] = None, start: Optional[float] = None,
               end: Optional[float] = None):
    """复制一个 pretty_midi Note（只碰音高/起点/终点，不动别的字段）。"""
    import pretty_midi

    return pretty_midi.Note(
        velocity=int(getattr(n, "velocity", 64) or 64),
        pitch=int(round(n.pitch if pitch is None else pitch)),
        start=float(n.start if start is None else start),
        end=float(n.end if end is None else end),
    )


def _pc_dist(a: float, b: float) -> float:
    """音高类距离（0~6），不受八度影响。"""
    return abs(((float(a) - float(b)) + 6.0) % 12.0 - 6.0)


# ---------------------------------------------------------------------------
# 1. 碎片合并 / 幽灵毛刺
# ---------------------------------------------------------------------------

def merge_voice_fragments(notes, frag_ms: float = 90.0,
                          join_gap_ms: float = 70.0
                          ) -> Tuple[List, Dict]:
    """合并同音碎片、删掉夹在两个同音之间的幽灵毛刺。

    两类形态（都是转写常见伪影，不是音乐）：
      · 同一个音被切成两段（中间有道小缝）→ 并回一个长音。
        只并「挨得近」的：**70ms 是音节检测的最小字头间隔**，缝小于它
        就不可能横跨两个真字（参考谱里 `Q Q` 这种唱完紧接着再唱同一个
        音的正常换字，字头间隔必然 ≥70ms，不会被并掉）。
      · 黑白键之间的幽灵音：C - C#(40ms) - C → C# 几乎不可能是唱出来的
        （人声一个音至少 90ms 才听得成），删掉，前后两个音各自保留。
    """
    evs = sorted(notes, key=lambda n: (float(n.start), int(n.pitch)))
    frag = max(0.0, frag_ms) / 1000.0
    join_gap = max(0.0, join_gap_ms) / 1000.0
    joined = 0

    merged: List = []
    for n in evs:
        if merged:
            prev = merged[-1]
            gap = float(n.start) - float(prev.end)
            if (int(n.pitch) == int(prev.pitch) and gap < join_gap
                    and float(n.start) - float(prev.start) < 1.0):
                prev.end = max(float(prev.end), float(n.end))
                joined += 1
                continue
        merged.append(_copy_note(n))

    ghosts = 0
    out: List = []
    for i, n in enumerate(merged):
        dur = float(n.end) - float(n.start)
        # 幽灵毛刺：夹在两个**同音**之间、时值短得起不来、差正好一个半音。
        # 不要求与前后音「挨得近」——短于 90ms 的音本来就不可能是一个字，
        # 300ms 外的短幽灵同样是转写噪声；真正该保留的快速经过音都有正常时值。
        if out and i + 1 < len(merged) and 0 < dur < frag:
            prev = out[-1]
            nxt = merged[i + 1]
            if (int(prev.pitch) == int(nxt.pitch)
                    and abs(int(n.pitch) - int(prev.pitch)) == 1):
                ghosts += 1
                continue
        out.append(n)

    return out, {"in": len(evs), "out": len(out),
                 "joined": joined, "ghosts": ghosts}


# ---------------------------------------------------------------------------
# 2. 局部 zigzag 修复（不需要音频，保守）
# ---------------------------------------------------------------------------

def repair_octave_spikes(notes, max_run: int = 2,
                         return_tol: float = 2.0) -> Tuple[List, Dict]:
    """修「跳出去又跳回来」的八度 zigzag（只换八度，音级完全不变）。

    规则（局部证据，不猜全局）：
      出现 ≥12 的跳进时向后看 —— 若旋律在 max_run 个音之内**回到起跳
      电平**（±return_tol 半音），就把这整个 excursion 平移回起跳电平
      （excursion 内部的相对音程保留）。没回来的不动：那可能是真实的
      乐句移动，没有音频证据时不猜。

    为什么不再做全局平滑：见模块 docstring「走过的弯路」——实测全局
    方法（Viterbi 带/不带主场先验）都会把中位偏差从 2.87 恶化到 7+。
    真正的八度仲裁靠 `correct_with_audio` 的实测基频。
    """
    evs = sorted(notes, key=lambda n: float(n.start))
    ps = [int(round(n.pitch)) for n in evs]
    n = len(ps)
    if n < 3:
        return list(evs), {"in": n, "repaired": 0}

    fixed = 0
    i = 1
    while i < n:
        jump = ps[i] - ps[i - 1]
        if abs(jump) < 12:
            i += 1
            continue
        base = ps[i - 1]
        # excursion：从 i 开始、持续贴在跳去电平上的音
        j = i
        while j + 1 < n and abs(ps[j + 1] - ps[i]) <= 2:
            j += 1
        run_len = j - i + 1
        # 跳出去之后是否回到起跳电平？
        if run_len <= max_run and j + 1 < n and abs(ps[j + 1] - base) <= return_tol:
            offset = base - ps[i]        #  excursion 整体挪回 base 电平
            for k in range(i, j + 1):
                ps[k] += offset
            fixed += run_len
            i = j + 1
        else:
            i += 1

    out = [_copy_note(note, pitch=p) for note, p in zip(evs, ps)]
    return out, {"in": n, "out": len(out), "repaired": fixed}


# ---------------------------------------------------------------------------
# 3. 有人声轨时的最终仲裁（主力）
# ---------------------------------------------------------------------------

def correct_with_audio(notes, syllables, tolerance_ms: float = 90.0,
                       snap_ms: float = 80.0, min_coverage: float = 0.5,
                       drop_short_ms: float = 150.0) -> Tuple[List, Dict]:
    """用分离人声轨的实测基频给旋律做最终仲裁（地面真相）。

    做三件事（覆盖率太低时整段跳过——音频与谱子大概不是同一首）：
      1. **八度按实测修正**：音级对得上（音高类距离 ≤1）但整个八度偏了
         → 按实测基频挪到正确八度。音级对不上的一律不碰（可能是真和声，
         交给后续黑键吸附处理；改音级是禁区，只换八度）。
      2. **剔除非人声/毛刺**：不在任何音节里的音（伴奏/间奏）删掉；
         音级也对不上（>2 半音）又短（<150ms）的删掉（转写毛刺）。
      3. **起点贴字头**：每个音节里最早的那个音，起点贴到检测到的字头
         （±80ms 内）——转写起点普遍偏晚，贴上去节奏才稳。

    返回 (新 notes, 统计)。统计里的 coverage 供调用方/界面判断
    「音频和谱子是不是同一首」。
    """
    evs = sorted(notes, key=lambda n: float(n.start))
    starts = [float(s.start) for s in syllables]
    tol = max(0.0, tolerance_ms) / 1000.0

    cover: List[Optional[int]] = []
    for note in evs:
        t = float(note.start)
        i = bisect.bisect_right(starts, t) - 1
        hit = None
        for j in (i, i + 1):
            if 0 <= j < len(syllables):
                s = syllables[j]
                if s.start - tol <= t <= s.end + tol:
                    hit = j
                    break
        cover.append(hit)

    cov = (sum(1 for c in cover if c is not None) / len(evs)) if evs else 0.0
    st: Dict[str, object] = {
        "syllables": len(syllables),
        "coverage": round(cov, 4),
        "octave_fixed": 0,
        "dropped_outside": 0,
        "dropped_mismatch": 0,
        "onset_snapped": 0,
        "applied": False,
    }
    if cov < min_coverage:
        st["skipped_reason"] = "音频与琴谱覆盖率过低（可能不是同一首），跳过校准"
        return list(evs), st
    st["applied"] = True

    short = max(0.0, drop_short_ms) / 1000.0
    rows: List[List] = []          # [note, syllable_index]
    for note, si in zip(evs, cover):
        if si is None:
            st["dropped_outside"] += 1
            continue
        p = int(round(note.pitch))
        dur = float(note.end) - float(note.start)
        syl = syllables[si]
        f0 = getattr(syl, "midi", None)
        if f0 is not None:
            f0 = float(f0)
            pcd = _pc_dist(p, f0)
            dev = abs(p - f0)
            # 音级**完全一致**（<0.5 半音）且整个八度偏了才修：
            # dev≥6 + pcd<0.5 ⇒ dev≈12 的整数倍，就是八度错误。
            # 差一个半音的（B  vs  C）不修 —— 那可能是真唱了半音之差，
            # 按八度修会造出一个更假的音。
            if pcd < 0.5 and dev >= 6.0:
                # 音级对得上、八度不对 → 按实测基频挪八度（只换八度）
                best = p
                while abs(best + 12 - f0) < abs(best - f0) - 1e-9:
                    best += 12
                while abs(best - 12 - f0) < abs(best - f0) - 1e-9:
                    best -= 12
                if best != p:
                    p = best
                    st["octave_fixed"] += 1
            elif pcd > 2.0 and dur < short:
                # 音级也对不上、又短 → 转写毛刺/伴奏音
                st["dropped_mismatch"] += 1
                continue
        rows.append([_copy_note(note, pitch=p), si])

    # 起点贴字头：每个音节里最早的那个音
    first_of: Dict[int, int] = {}
    for i, (_note, si) in enumerate(rows):
        if si is None:
            continue
        if si not in first_of or rows[i][0].start < rows[first_of[si]][0].start:
            first_of[si] = i
    snap_s = max(0.0, snap_ms) / 1000.0
    for si, i in first_of.items():
        note = rows[i][0]
        target = float(syllables[si].start)
        if abs(note.start - target) > snap_s:
            continue
        floor = rows[i - 1][0].start + 0.02 if i > 0 else 0.0
        new_t = min(max(target, floor), note.end - 0.02)
        if abs(new_t - note.start) > 1e-6:
            note.start = new_t
            st["onset_snapped"] += 1

    st["in"] = len(evs)
    st["out"] = len(rows)
    return [r[0] for r in rows], st


# ---------------------------------------------------------------------------
# 总入口：人声分支在抽主旋律之后调用一次
# ---------------------------------------------------------------------------

def prepare_voice_melody(notes, ak, audio_path=None):
    """人声分支专用：碎片合并 →（有音频）基频仲裁 → 局部 zigzag 修复。

    音频仲裁放在局部修复之前：实测基频是地面真相，先按它修；局部修复
    只收拾音频管不到的区域（没有音节覆盖的音），而且只动「跳出去又
    跳回来」的高置信度形态。全局重排（Viterbi 等）实测有害，不用。
    """
    stats: Dict[str, object] = {}
    notes, st = merge_voice_fragments(notes)
    stats["fragments"] = st

    if audio_path:
        try:
            from .vocal_detect import detect_syllables

            syllables = detect_syllables(str(audio_path))
            notes, st = correct_with_audio(notes, syllables)
            stats["audio"] = st
        except Exception as exc:  # noqa: BLE001  音频读不了/检测失败不该阻断映射
            logger.warning("人声轨校准失败，退回纯 MIDI 处理：%s", exc)
            stats["audio"] = {"applied": False, "error": str(exc)}

    notes, st = repair_octave_spikes(notes)
    stats["octave_spikes"] = st
    return notes, stats
