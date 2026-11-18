"""风物之诗琴自动化工具 · 配置

集中管理路径、BPM、点击延迟，以及**默认乐器**的键位表。

键位表本身不再写死在这里，而是从 `piano_tool.instruments` 取：
每件乐器（风物之诗琴 21 键 / 首席女高音 14 键）各有一份「按键 → 音高」。
下面这些常量是**默认乐器**（风物之诗琴）的只读快照，供老代码继续按老名字用；
需要按用户选中的乐器算音域时，请读 `instruments.active()`。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from pydantic import BaseModel, ConfigDict

from .instruments import (
    DEFAULT_INSTRUMENT_ID,
    Instrument,
    active,
    get_instrument,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 默认乐器（风物之诗琴）的键位快照：C4(60) ~ B6(95)，共 21 个白键。
# 键位布局（键盘按键 -> 音高）与原神「风物之诗琴」社区模拟器一致。
_DefaultInstrument: Instrument = get_instrument(DEFAULT_INSTRUMENT_ID)

# 注意：这里做一次浅拷贝，避免外部改到乐器注册表里那份共享表。
WHITE_KEYS: List[Dict[str, object]] = [dict(k) for k in _DefaultInstrument.keys]

KEY_BY_MIDI: Dict[int, str] = dict(_DefaultInstrument.key_by_midi)
NAME_BY_MIDI: Dict[int, str] = {
    int(k["midi"]): str(k["name"]) for k in WHITE_KEYS
}
MIDI_BY_KEY: Dict[str, int] = dict(_DefaultInstrument.midi_by_key)
WHITE_MIDIS: List[int] = list(_DefaultInstrument.white_midis)
LOWEST_MIDI = _DefaultInstrument.lowest_midi
HIGHEST_MIDI = _DefaultInstrument.highest_midi

__all__ = [
    "Config",
    "HIGHEST_MIDI",
    "KEY_BY_MIDI",
    "LOWEST_MIDI",
    "MIDI_BY_KEY",
    "NAME_BY_MIDI",
    "PROJECT_ROOT",
    "WHITE_KEYS",
    "WHITE_MIDIS",
    "active",
    "get_config",
    "get_instrument",
]


class Config(BaseModel):
    """全局配置。路径相对于项目根目录。"""

    bpm: int = 120
    click_delay_ms: int = 20
    default_speed: float = 1.0

    data_dir: Path = PROJECT_ROOT / "data"
    input_dir: Path = PROJECT_ROOT / "data" / "input"
    output_dir: Path = PROJECT_ROOT / "data" / "output"
    calibration_path: Path = PROJECT_ROOT / "data" / "calibration.json"
    # ③ 页在「键盘映射」与「屏幕校准」之间切换时，屏幕校准资料的自动备份
    screen_calibration_backup: Path = PROJECT_ROOT / "data" / "calibration_screen_backup.json"
    latency_profile_path: Path = PROJECT_ROOT / "data" / "key_latency.json"
    debug_image: Path = PROJECT_ROOT / "data" / "debug_calibration.png"
    editor_path: Path = PROJECT_ROOT / "editor" / "editor.html"
    report_path: Path = PROJECT_ROOT / "data" / "simulator_test_report.json"

    model_config = ConfigDict(arbitrary_types_allowed=True)


def get_config() -> Config:
    """返回一份默认配置（可按需覆盖字段）。"""
    return Config()
