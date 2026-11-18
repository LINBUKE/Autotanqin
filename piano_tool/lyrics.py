"""人声匹配 —— 把中文歌词配到已有旋律上，做到「一个汉字一个音」。

为什么人声非得这样
------------------
拨弦乐器一个音可以随便切、随便叠；人声不一样：**一个字就是一个音节，一个音节
只唱一个音**。所以自动映射出来的旋律只要出现下面两种情况，听起来就不像人唱：

  · 同一个字被转写拆成好几段（拖腔 / 毛刺）→ 像在同一个字里又弹了一次
  · 音符数和歌词字数对不上 → 有的字没唱、有的字被唱了两遍

本模块只做「对齐」：给每个音符标上它唱的哪个字，并在字数与音符数不一致时按用户
选定的策略合并 / 拆分。**不凭空改调、不重新生成旋律** —— 音高仍然来自 MIDI。

对齐规则
--------
  · 歌词里的标点、空格不占音（它们只表示断句）
  · 音比字多：把相邻最近的音并进同一个字（拖腔/毛刺都是挨着的），
    字多音少：把最长的音拆成两段（长音才拆得动，短音不动）
  · 合并时「取哪个音高」受声调影响（可选）：
      一声（平）→ 首音   二声（升）→ 组内最高
      三声（降升）→ 组内中间音   四声（降）→ 组内最低
    这只是**在已有的几个音里挑一个**，不会生成新音高。
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Sequence, Tuple

from .instruments import active
from .models import NoteEvent

logger = logging.getLogger("piano_tool.lyrics")

__all__ = [
    "align_one_char_one_note",
    "compare_counts",
    "parse_lyrics",
    "tone_available",
    "tone_of",
]

# 标点 / 空格 / 换行：不占音，只当断句看
_PUNCT = set(
    "，。！？、；：…—·,.!?;:\"'“”‘’（）()《》〈〉【】[]{}<>-—_~～ \t\r\n\u3000"
)

# 纯 ASCII 标点之外，CJK、字母、数字都算一个音节
_LETTER_RE = re.compile(r"[0-9A-Za-z\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff]")


def _is_syllable(ch: str) -> bool:
    if ch in _PUNCT:
        return False
    return bool(_LETTER_RE.match(ch))


def parse_lyrics(text: str) -> List[str]:
    """把歌词文本拆成「一个音节一个字」的列表；标点与空白直接跳过。"""
    return [ch for ch in str(text or "") if _is_syllable(ch)]


def tone_available() -> bool:
    """能不能查声调（需要 pypinyin；没装就自动降级为「不参与声调」）。"""
    try:
        import pypinyin  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


_TONE_CACHE: Dict[str, Optional[int]] = {}


def tone_of(ch: str) -> Optional[int]:
    """一个字的声调：1~4，轻声返回 5；查不到返回 None。"""
    if ch in _TONE_CACHE:
        return _TONE_CACHE[ch]
    tone: Optional[int] = None
    try:
        from pypinyin import Style, lazy_pinyin

        py = lazy_pinyin(ch, style=Style.TONE3, neutral_tone_with_five=True)
        if py:
            m = re.search(r"([1-5])$", py[0])
            if m:
                tone = int(m.group(1))
    except Exception:  # noqa: BLE001  没装 pypinyin / 非汉字
        tone = None
    _TONE_CACHE[ch] = tone
    return tone


def compare_counts(events: Sequence, lyrics: Sequence[str]) -> Dict[str, int]:
    """字数和音符数的对比，给界面弹窗用。"""
    n = len(events)
    k = len(lyrics)
    return {"notes": n, "chars": k, "diff": n - k}


def _group_value(group: List, tone: Optional[int]) -> int:
    """一组音（同一个字）该取哪个音高。

    tone 给了就按声调走向挑；没给 / 一组只有一个音就取首音（最忠于原曲的起音）。
    """
    if len(group) <= 1 or not tone:
        return int(group[0].note)
    notes = [int(e.note) for e in group]
    if tone == 2:      # 二声：升，取最高
        return max(notes)
    if tone == 4:      # 四声：降，取最低
        return min(notes)
    if tone == 3:      # 三声：降升，取中间
        return sorted(notes)[len(notes) // 2]
    return int(group[0].note)   # 一声 / 轻声


def _make(group: List, char: str, tone: Optional[int]) -> NoteEvent:
    """把一组音合成「一个字」对应的一个音。"""
    first = group[0]
    last = group[-1]
    start = float(first.time)
    end = float(last.time) + float(last.duration or 0.0)
    midi = _group_value(group, tone)
    return NoteEvent(
        time=first.time,
        note=midi,
        key=active().key_by_midi.get(midi, first.key),
        action=first.action,
        duration=max(0.0, round(end - start, 4)),
        lyric=char,
    )


def _merge_to(events: List, k: int) -> Tuple[List[List], int]:
    """把音分组，使组数 = k。每次合并「时间上挨得最近」的相邻两段。

    拖腔 / 转写毛刺都是紧挨着的，按「最近」合并最符合音乐直觉；
    不按位置平均分，因为歌词本身没有时间信息，平均分反而会拆散一句话。
    """
    groups: List[List] = [[e] for e in events]
    merged = 0
    while len(groups) > k:
        best_gap = None
        best_i = 0
        for i in range(len(groups) - 1):
            a, b = groups[i][-1], groups[i + 1][0]
            gap = float(b.time) - (float(a.time) + float(a.duration or 0.0))
            if best_gap is None or gap < best_gap:
                best_gap = gap
                best_i = i
        groups[best_i] = groups[best_i] + groups[best_i + 1]
        del groups[best_i + 1]
        merged += 1
    return groups, merged


def _split_to(events: List, k: int, min_hold: float) -> Tuple[List, int]:
    """把最长的音一段段拆开，使音符数 = k。短于两倍最小按住的音拆不动，跳过。"""
    out: List = list(events)
    split = 0
    while len(out) < k:
        idx = -1
        longest = 0.0
        for i, e in enumerate(out):
            d = float(e.duration or 0.0)
            if d >= 2 * min_hold and d > longest:
                longest = d
                idx = i
        if idx < 0:
            break
        e = out[idx]
        d = float(e.duration or 0.0)
        half = round(d / 2.0, 4)
        midi = int(e.note)
        out[idx] = NoteEvent(
            time=e.time, note=midi, key=e.key, action=e.action,
            duration=half, lyric=getattr(e, "lyric", None),
        )
        out.insert(idx + 1, NoteEvent(
            time=round(float(e.time) + half, 4), note=midi, key=e.key,
            action=e.action, duration=round(d - half, 4),
            lyric=getattr(e, "lyric", None),
        ))
        split += 1
    return out, split


def align_one_char_one_note(
    events: Sequence,
    lyrics: Sequence[str],
    policy: str = "merge",
    use_tone: bool = False,
    min_hold_ms: float = 90.0,
) -> Tuple[List[NoteEvent], Dict]:
    """把歌词按顺序配给音符，返回 (新 events, 统计)。

    policy（只在字数与音符数不一致时起作用）：
        "merge"         音比字多 → 把多余的音并进相邻的字（推荐）
        "drop"          音比字多 → 丢掉多余的音（保留较长的、丢最短的）
        "split"         字比音多 → 拆长音补足
        "ignore_chars"  字比音多 → 多出来的字不配（只配前 n 个）
        "cancel"        不处理

    注意：所有操作都**不改调**。合并时只在组内已有音高里挑一个；
    拆分只是把同一个音高的时值分成两段。
    """
    evs = sorted(events, key=lambda e: e.time)
    chars = list(lyrics)
    st: Dict[str, object] = {
        "notes_in": len(evs), "chars": len(chars), "policy": policy,
        "tone_used": bool(use_tone and tone_available()),
        "merged": 0, "split": 0, "dropped": 0, "ignored_chars": 0,
        "tone_conflicts": 0, "notes_out": 0,
    }
    if policy == "cancel" or not chars:
        st["notes_out"] = len(evs)
        return evs, st

    n, k = len(evs), len(chars)
    tone_on = bool(st["tone_used"])

    if n > k:
        if policy == "drop":
            # 丢最短的，保持时间顺序（用户明确选择时才这么做）
            keep = sorted(range(n), key=lambda i: (-float(evs[i].duration or 0.0), i))[:k]
            evs = [evs[i] for i in sorted(keep)]
            st["dropped"] = n - k
        else:
            groups, merged = _merge_to(evs, k)
            st["merged"] = merged
            st["notes_out"] = 0
            out = [_make(g, ch, tone_of(ch) if tone_on else None)
                   for g, ch in zip(groups, chars)]
            st["notes_out"] = len(out)
            st["tone_conflicts"] = _count_conflicts(out, tone_on)
            return out, st
    elif k > n:
        if policy != "split":
            st["ignored_chars"] = k - n
            chars = chars[:n]
        else:
            evs, split = _split_to(evs, k, max(min_hold_ms, 1.0) / 1000.0)
            st["split"] = split
            if len(evs) < k:
                st["ignored_chars"] = k - len(evs)
                chars = chars[:len(evs)]

    # 数量已经对齐（或用户选择忽略多余的字）
    out = [
        NoteEvent(
            time=e.time, note=e.note, key=e.key, action=e.action,
            duration=e.duration, lyric=ch,
        )
        for e, ch in zip(evs, chars)
    ]
    st["notes_out"] = len(out)
    st["tone_conflicts"] = _count_conflicts(out, tone_on)
    return out, st


def _count_conflicts(events: List[NoteEvent], tone_on: bool) -> int:
    """声调与旋律走向明显冲突的地方（只报数，不改音）。

    二声该往上、四声该往下。旋律反着走时记一次 —— 这不是错误，
    中文歌里「倒字」很常见，只是让用户知道哪些字可能听着别扭。
    """
    if not tone_on or len(events) < 2:
        return 0
    conflicts = 0
    for i in range(1, len(events)):
        tone = tone_of(str(getattr(events[i], "lyric", "") or ""))
        d = int(events[i].note) - int(events[i - 1].note)
        if d == 0:
            continue
        if tone == 2 and d < 0:
            conflicts += 1
        elif tone == 4 and d > 0:
            conflicts += 1
    return conflicts
