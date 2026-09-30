"""风物之诗琴自动化工具 · 包入口"""
from .config import (
    Config,
    WHITE_KEYS,
    KEY_BY_MIDI,
    NAME_BY_MIDI,
    MIDI_BY_KEY,
    WHITE_MIDIS,
    LOWEST_MIDI,
    HIGHEST_MIDI,
    get_config,
)
from .models import NoteEvent, KeyCoordinate, Calibration, MappingConfig

__version__ = "0.1.0"

__all__ = [
    "Config",
    "WHITE_KEYS",
    "KEY_BY_MIDI",
    "NAME_BY_MIDI",
    "MIDI_BY_KEY",
    "WHITE_MIDIS",
    "LOWEST_MIDI",
    "HIGHEST_MIDI",
    "get_config",
    "NoteEvent",
    "KeyCoordinate",
    "Calibration",
    "MappingConfig",
]
