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
    4. 黑键就近替换为同音区白键
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


def snap_to_white(midi: int) -> Tuple[int, bool]:
    """把任意 MIDI 音高映射到最近的 21 白键之一（含八度折叠）。

    返回 (目标白键 MIDI, 是否发生了替换/折叠)。
    """
    folded = midi
    folded_in_range = LOWEST_MIDI <= folded <= HIGHEST_MIDI
    while folded < LOWEST_MIDI:
        folded += 12
    while folded > HIGHEST_MIDI:
        folded -= 12

    if folded in WHITE_MIDIS:
        replaced = (not folded_in_range) or (folded != midi)
        return folded, replaced

    # 黑键：找最近的白键
    best = min(WHITE_MIDIS, key=lambda w: abs(w - folded))
    return best, True


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


def _white_hit_rate(notes, shift: int) -> float:
    total = 0.0
    hit = 0.0
    for n in notes:
        w = max(float(n.end - n.start), 0.05)
        total += w
        if (int(round(n.pitch)) + shift) % 12 in WHITE_PITCH_CLASSES:
            hit += w
    return hit / total if total else 0.0


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
) -> Tuple[List[NoteEvent], Dict]:
    """读取 MIDI 并映射为按键事件列表 + 统计信息。"""
    import pretty_midi

    pm = pretty_midi.PrettyMIDI(str(midi_path))
    raw = [n for inst in pm.instruments for n in inst.notes]
    notes = clean_notes(raw, min_duration=min_duration, dedupe_gap=dedupe_gap)
    if mono:
        notes = pick_melody(notes, mode=melody_mode)

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
    mapped = []
    dropped_range = 0
    for n in notes:
        orig = int(round(n.pitch)) + shift
        in_range = LOWEST_MIDI <= orig <= HIGHEST_MIDI
        if not in_range and not fold_out_of_range:
            # 本琴只有 21 键，超音域的多半是低音贝斯；折叠上来的话会突然
            # 出现在旋律音区、听起来很乱，因此提供「直接丢弃」的选项。
            dropped_range += 1
            continue
        target, replaced = snap_to_white(orig)
        mapped.append((float(n.start), target, orig, replaced,
                       float(max(0.05, n.end - n.start))))

    filtered: List = []
    last_hit: Dict[int, float] = {}
    for item in mapped:
        t, target = item[0], item[1]
        prev = last_hit.get(target)
        if prev is not None and t - prev < same_key_gap:
            continue
        last_hit[target] = t
        filtered.append(item)
    dup_removed = len(mapped) - len(filtered)

    events: List[NoteEvent] = []
    stats = {
        "total": 0,
        "raw_total": len(raw),
        "dropped": len(raw) - len(notes) + dup_removed + dropped_range,
        "dropped_range": dropped_range,
        "dup_removed": dup_removed,
        "mono": bool(mono),
        "black_replaced": 0,  # 黑键被吸附到相邻白键的数量（会改变音高）
        "octave_folded": 0,   # 超出范围被八度折叠的数量（音级不变）
        "white_kept": 0,      # 范围内白键直接保留的数量
        "transpose": shift,
        "white_hit_rate": None if hit_rate is None else round(hit_rate, 4),
    }

    for t, target, orig, replaced, dur in filtered:
        stats["total"] += 1
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

    events.sort(key=lambda e: e.time)
    logger.info(
        "映射完成：%d 音符（原始 %d，清理 %d）→ 移调 %+d 半音，白键命中 %.1f%%，"
        "黑键替换 %d，八度折叠 %d，白键保留 %d",
        stats["total"], stats["raw_total"], stats["dropped"], shift,
        (hit_rate or 0) * 100, stats["black_replaced"],
        stats["octave_folded"], stats["white_kept"],
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
