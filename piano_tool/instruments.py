"""乐器表 —— 每个乐器就是一组「键盘按键 → MIDI 音高」的映射 + 音域。

目前两件乐器（都是 C 大调白键音阶，没有黑键）：

风物之诗琴（工具默认，21 键，C4~B6）
    底行 Z X C V B N M = C4 ~ B4
    中行 A S D F G H J = C5 ~ B5
    顶行 Q W E R T Y U = C6 ~ B6

首席女高音（14 键，C4~B5，只有上两排发声）
    下排 A S D F G H J = C4 ~ B4
    上排 Q W E R T Y U = C5 ~ B5

同一个字母键在两件乐器上发出的音**不一样**（风物之诗琴的 A 是 C5，
首席女高音的 A 是 C4），所以琴谱必须按乐器分别生成，不能混用。

首席女高音的音高不是猜的，是把采集到的 14 个音色做 FFT 实测出来的
（下排 do 261.5Hz / re 293.7 / mi 329.6 / fa 349.4 / so 391.8 / la 440.2 /
ti 494.4；上排 do 523.0 / re 587.4 / mi 659.2 / fa 698.7 / so 784.4 /
la 879.6 / ti 988.0）：下排正好 C4~B4，上排正好是它高八度 C5~B5，
每个音都落在 1 音分以内。采集样本里能看到明显颤音，与人声类乐器相符。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# C 大调音阶在白键上的半音偏移（do re mi fa so la ti）
_SCALE = (0, 2, 4, 5, 7, 9, 11)

_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

DEFAULT_INSTRUMENT_ID = "lyre"


def midi_name(midi) -> str:
    """MIDI 音高 → 音名，60 → 'C4'（与 config 里原来的命名保持一致）。"""
    m = int(midi)
    return f"{_NOTE_NAMES[m % 12]}{m // 12 - 1}"


@dataclass(frozen=True)
class Row:
    """一排琴键：键盘字母串 + 这一排最低音的 MIDI 音高。"""

    label: str
    keys: str
    base_midi: int

    @property
    def letters(self) -> List[str]:
        return list(self.keys)

    @property
    def lo(self) -> int:
        return int(self.base_midi)

    @property
    def hi(self) -> int:
        return int(self.base_midi) + 11

    def desc(self) -> str:
        return f"{self.label} {' '.join(self.letters)} = {midi_name(self.lo)}~{midi_name(self.hi)}"


@dataclass
class Instrument:
    """一件乐器。派生表在 __post_init__ 里一次算好，后面都是查表。"""

    id: str
    name: str
    rows: Tuple[Row, ...]        # 按音高从低到高排列
    hint: str = ""               # 界面上的一句话说明
    source: str = ""             # 音高依据

    # ---- 人声 / 单声部乐器的映射预设（见 preset()）----
    # 人声一次只能发一个音，而且「旋律轮廓像不像原曲」远比「音准精确」重要；
    # 所以照搬拨弦/键盘乐器那套「多声部 + 八度折叠 + 分层」会糊成一团、刺耳。
    monophonic: bool = False
    # 舒服音区：尽量把旋律放在这一段里，而不是顶满整个音域
    comfort_band: Optional[Tuple[int, int]] = None
    # 旋律最大跳进（半音）：人声不会动不动跳两个八度
    comfort_max_leap: Optional[int] = None
    # 「柔化」时的上限音。和 comfort_band 分开：comfort_band 是「移调时优先落哪一段」，
    # 这个是「高到哪个音才降八度」。人声参考谱 90% 在 C4~C5，但偶尔会用到 C5~E5，
    # 所以不能把 C5 当上限，否则那些正常的高音会被硬降下来。
    soft_ceiling: Optional[int] = None

    # ---- 发声学（从采集音色里实测，用来决定琴谱怎么写「时值」）----
    # 拨弦乐器一碰就响、很快衰减；人声有起音过程、会一直保持，而且带颤音。
    # 这些数字不是猜的，是 G:\...\Yinpincaiji\samples 的 14 个音色量出来的。
    attack_ms: Optional[float] = None        # 起音到峰值
    natural_decay_ms: Optional[float] = None  # 自然衰减到 -20dB
    vibrato_hz: Optional[float] = None       # 颤音速率
    vibrato_cents: Optional[float] = None    # 颤音深度（音分）
    min_hold_ms: Optional[float] = None      # 一个音至少按住多久才听得成「唱」
    legato_gap_ms: Optional[float] = None    # 相邻音空隙小于此值就唱连，不重新起音
    articulation_max_gap_ms: Optional[float] = None  # 超过此值的停顿保留为换气
    same_pitch_overlap_ms: Optional[float] = None    # 同音「叠着响」超过这么多就并成一个长音

    keys: List[Dict[str, object]] = field(default_factory=list, init=False)
    key_by_midi: Dict[int, str] = field(default_factory=dict, init=False)
    midi_by_key: Dict[str, int] = field(default_factory=dict, init=False)
    white_midis: List[int] = field(default_factory=list, init=False)
    lowest_midi: int = field(default=0, init=False)
    highest_midi: int = field(default=0, init=False)

    def __post_init__(self):
        keys: List[Dict[str, object]] = []
        for row in self.rows:
            for letter, semi in zip(row.letters, _SCALE):
                m = row.lo + semi
                keys.append({"midi": m, "key": letter, "name": midi_name(m)})
        keys.sort(key=lambda k: int(k["midi"]))
        self.keys = keys
        self.key_by_midi = {int(k["midi"]): str(k["key"]) for k in keys}
        self.midi_by_key = {str(k["key"]): int(k["midi"]) for k in keys}
        self.white_midis = sorted(int(k["midi"]) for k in keys)
        self.lowest_midi = self.white_midis[0]
        self.highest_midi = self.white_midis[-1]

    # ---- 给界面用的文案 ----
    @property
    def row_count(self) -> int:
        return len(self.rows)

    def range_desc(self) -> str:
        return (f"{midi_name(self.lowest_midi)}~{midi_name(self.highest_midi)}"
                f"（{self.lowest_midi}~{self.highest_midi}，{len(self.keys)} 键）")

    def rows_desc(self, high_first: bool = True) -> str:
        """一排一句话，默认高音排在前（与琴面画法一致）。"""
        rows = list(reversed(self.rows)) if high_first else list(self.rows)
        return "；".join(r.desc() for r in rows)

    def octave_bands(self) -> List[List[int]]:
        """分层用的八度带 —— 一排琴键一段，正好就是键盘上那一行。

        风物之诗琴 3 排 → [60,71] / [72,83] / [84,95]（与老公式结果完全一致）；
        首席女高音 2 排 → [60,71] / [72,83]。
        用「按排切」而不是「按音域等分」是因为这个功能的用意就是让低音/中音/
        高音各自落在键盘对应的那一行上，排数对了带才对得上键。
        """
        return [[r.lo, r.hi] for r in self.rows]

    # ---- ② 页换乐器时用的映射预设 ----
    def preset(self) -> Dict[str, object]:
        """这件乐器的「该怎么映射」预设，供界面换乐器时切默认参数。

        风物之诗琴（拨弦，能同时发多个音）用老一套：多声部、开分层、超音域折叠。
        首席女高音（人声，只能发一个音）用另一套：只取主旋律、关分层、超音域
        先按八度挪进音域（挪不进的才丢）、跳进收紧。

        键的用途：
          mono / melody_mode / fold_out_of_range / max_leap / ceiling
                 —— 界面换乐器时填到 ② 页那些控件上（用户随后可自行改）；
                 其中 mono / melody_mode / fold_out_of_range 同时是
                 `midi_mapper.map_midi_to_events` 在参数传 None 时的默认值。
          layered —— 只给界面用（分层开关在 ② 页，mapper 的默认值仍是 False）。
        """
        return {
            "mono": bool(self.monophonic),
            # 人声用「连续人声线」抽旋律（跟上一个音接轨，不被伴奏尖音带跑）；
            # 拨弦乐器保持老默认 loud。
            "melody_mode": "vocal" if self.monophonic else "loud",
            "layered": not self.monophonic,
            "fold_out_of_range": not self.monophonic,
            # 折叠范围：逐音把旋律硬塞进音域会在边界处制造八度跳，
            # 人声宁可少几个超范围的音，也不要那种跳。
            "max_leap": int(self.comfort_max_leap) if self.comfort_max_leap else 12,
            "ceiling": (int(self.soft_ceiling) if self.soft_ceiling
                        else (int(self.comfort_band[1]) if self.comfort_band else None)),
            "min_hold_ms": self.min_hold_ms,
            "legato_gap_ms": self.legato_gap_ms,
        }

    def articulation_desc(self) -> str:
        """一句话说清「这个乐器该怎么唱」（用在 ② 页日志里）。"""
        if not self.monophonic or not self.min_hold_ms:
            return ""
        parts = []
        if self.attack_ms:
            parts.append(f"起音 {self.attack_ms:.0f}ms")
        if self.natural_decay_ms:
            parts.append(f"自然衰减 {self.natural_decay_ms:.0f}ms")
        if self.vibrato_hz:
            parts.append(f"颤音 {self.vibrato_hz:.1f}Hz/{self.vibrato_cents:.0f}音分")
        body = "、".join(parts)
        return (f"实测发声学：{body} → 每个音至少按住 {self.min_hold_ms:.0f}ms"
                f"（短了只有气口听不成音），相邻音空隙 <{self.legato_gap_ms:.0f}ms 就唱连、"
                f"不重新起音；停顿超过 {self.articulation_max_gap_ms:.0f}ms 保留为换气")


_INSTRUMENTS: Dict[str, Instrument] = {}


def _register(inst: Instrument) -> Instrument:
    _INSTRUMENTS[inst.id] = inst
    return inst


# ---------------------------------------------------------------------------
# 乐器定义
# ---------------------------------------------------------------------------
LYRE = _register(Instrument(
    id="lyre",
    name="风物之诗琴",
    rows=(
        Row("底行", "ZXCVBNM", 60),   # C4~B4
        Row("中行", "ASDFGHJ", 72),   # C5~B5
        Row("顶行", "QWERTYU", 84),   # C6~B6
    ),
    hint="21 键，三排键盘各一个八度，音域最宽。",
    source="与原神「风物之诗琴」社区模拟器键位一致。",
))

SOPRANO = _register(Instrument(
    id="soprano",
    name="首席女高音",
    rows=(
        Row("下排", "ASDFGHJ", 60),   # C4~B4
        Row("上排", "QWERTYU", 72),   # C5~B5
    ),
    hint="14 键，只有上两排发声，音域比风物之诗琴少一个高八度。人声，一次只能唱一个音。",
    source="音高由采集的 14 个音色 FFT 实测得出（下排 C4~B4、上排 C5~B5）。",
    # 人声三件事：只能单音、旋律轮廓比音准重要、跳进要小。
    # 音域只有两个八度，低音贝斯一旦折上来就和人声挤在一起 —— 所以超音域直接丢。
    monophonic=True,
    # 舒服音区按用户给的四份参考谱量出来（共 451 音）：
    #   音域合起来 C4~A5；中位 A4；90% 在 E5 以内，95% 在 F5 以内。
    # 四首各自的中位（G4 / D5 / E4 / C5）全部落在这一段，所以拿它做「整体归位」的判据。
    comfort_band=(60, 76),
    # 「柔和化」允许的最大跳进：四份谱实测最大 10 半音（六度/小七度，是人声常见的
    # 真跳进，不是拼接错误）。设成 7 会把它们误判成「跳跃过硬」去降八度，反而改坏旋律。
    comfort_max_leap=10,
    # 柔化上限 = A5：四份谱 451 个音最高就是 A5，100% 都在这个线以内。
    # 设更严（像我之前设的 G5）会把这 3 个 A5 硬降成 A4，旋律轮廓就断了。
    soft_ceiling=81,
    # 以下全是从 samples/ 里 14 个音色实测的中位数（见 acoustics 分析）：
    attack_ms=35.0,             # 起音到峰值 35ms（低音更慢，最长 150ms）
    natural_decay_ms=938.0,     # 自然衰减到 -20dB 要 0.94 秒 —— 人声是「撑住」的
    vibrato_hz=4.6,             # 4.6Hz 微颤
    vibrato_cents=5.0,          # 深度约 5 音分（很轻，但确实在动）
    # 90ms = 起音 35ms + 余量：短于这个只有「气口」，听不成一个音
    min_hold_ms=90.0,
    # 空隙 <60ms 说明这句是连唱，不该重新起音（人声换字也不会留这么短的气口）
    legato_gap_ms=60.0,
    # 超过 150ms 的停顿是换气/断句，必须保留，不能拿延音糊过去
    articulation_max_gap_ms=150.0,
    # 同一个音**还叠着响**的时候又被触发：人声做不到在一个音里再起一次音，
    # 会听成卡了一下 —— 并成一个长音。
    # 注意只并「叠着的」，不并「前一个已经结束、紧接着唱下一个同音」——
    # 参考谱里有直接写 `Q Q` 这种刻意重复的同音，那是正常的换字，不该吞掉。
    same_pitch_overlap_ms=5.0,
))


def all_instruments() -> List[Instrument]:
    """按注册顺序返回全部乐器。"""
    return list(_INSTRUMENTS.values())


def get_instrument(spec=None) -> Instrument:
    """按 id 或名称取乐器；认不出来（含 None/空）就返回默认那件。

    也接受「首席女高音」这类中文名，方便界面直接传显示文本。
    """
    if isinstance(spec, Instrument):
        return spec
    key = str(spec or "").strip()
    if not key:
        return _INSTRUMENTS[DEFAULT_INSTRUMENT_ID]
    if key in _INSTRUMENTS:
        return _INSTRUMENTS[key]
    for inst in _INSTRUMENTS.values():
        if inst.name == key:
            return inst
    return _INSTRUMENTS[DEFAULT_INSTRUMENT_ID]


# ---------------------------------------------------------------------------
# 当前生效的映射
#
# midi_mapper / locator / keyboard_play / simulator_test 都要知道「这次按哪件
# 乐器算音域」，但它们的公开函数签名以前是固定 21 键的（cli、测试都在用），
# 不适合逐个加参数。所以这里放一个进程级「当前乐器」，各处统一读它：
# 界面上选了乐器 → use(乐器) → 各模块按新音域工作。
# 工具是单人单进程、一次只跑一个操作，所以不需要加锁。
# ---------------------------------------------------------------------------
class ActiveKeys:
    """当前生效的乐器。`use()` 切乐器，`get()` 取当前。"""

    _current: Instrument = _INSTRUMENTS[DEFAULT_INSTRUMENT_ID]

    @classmethod
    def use(cls, spec=None) -> Instrument:
        cls._current = get_instrument(spec)
        return cls._current

    @classmethod
    def get(cls) -> Instrument:
        return cls._current

    @classmethod
    def reset(cls) -> Instrument:
        cls._current = _INSTRUMENTS[DEFAULT_INSTRUMENT_ID]
        return cls._current


def use(spec=None) -> Instrument:
    """切换当前乐器（返回切过去的那个 Instrument）。"""
    return ActiveKeys.use(spec)


def active() -> Instrument:
    """当前乐器（各模块算音域时统一读这里）。"""
    return ActiveKeys.get()
