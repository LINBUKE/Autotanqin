"""风物之诗琴自动化工具 · 配置

集中管理路径、BPM、点击延迟，以及 21 白键布局。
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from pydantic import BaseModel, ConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 21 个白键：C4(60) ~ B6(95)。
# 键位布局（键盘按键 -> 音高）与原神「风物之诗琴」社区模拟器一致。
WHITE_KEYS: List[Dict[str, object]] = [
    {"midi": 60, "key": "Z", "name": "C4"},
    {"midi": 62, "key": "X", "name": "D4"},
    {"midi": 64, "key": "C", "name": "E4"},
    {"midi": 65, "key": "V", "name": "F4"},
    {"midi": 67, "key": "B", "name": "G4"},
    {"midi": 69, "key": "N", "name": "A4"},
    {"midi": 71, "key": "M", "name": "B4"},
    {"midi": 72, "key": "A", "name": "C5"},
    {"midi": 74, "key": "S", "name": "D5"},
    {"midi": 76, "key": "D", "name": "E5"},
    {"midi": 77, "key": "F", "name": "F5"},
    {"midi": 79, "key": "G", "name": "G5"},
    {"midi": 81, "key": "H", "name": "A5"},
    {"midi": 83, "key": "J", "name": "B5"},
    {"midi": 84, "key": "Q", "name": "C6"},
    {"midi": 86, "key": "W", "name": "D6"},
    {"midi": 88, "key": "E", "name": "E6"},
    {"midi": 89, "key": "R", "name": "F6"},
    {"midi": 91, "key": "T", "name": "G6"},
    {"midi": 93, "key": "Y", "name": "A6"},
    {"midi": 95, "key": "U", "name": "B6"},
]

KEY_BY_MIDI: Dict[int, str] = {int(k["midi"]): str(k["key"]) for k in WHITE_KEYS}
NAME_BY_MIDI: Dict[int, str] = {int(k["midi"]): str(k["name"]) for k in WHITE_KEYS}
MIDI_BY_KEY: Dict[str, int] = {str(k["key"]): int(k["midi"]) for k in WHITE_KEYS}
WHITE_MIDIS: List[int] = sorted(int(k["midi"]) for k in WHITE_KEYS)
LOWEST_MIDI = WHITE_MIDIS[0]
HIGHEST_MIDI = WHITE_MIDIS[-1]


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
