"""风物之诗琴自动化工具 · 数据模型

所有模块共享的 Pydantic 数据模型。
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

# 弹奏方式（③ 四点校准页的两个单选项）
INPUT_MODE_SCREEN = "screen"      # 屏幕校准：截图 + 四点 → 21 键坐标 → 鼠标点击
INPUT_MODE_KEYBOARD = "keyboard"  # 键盘映射：Q→U / A→J / Z→M 三行 21 键 → 键盘按键


class NoteEvent(BaseModel):
    """单个按键事件（events.json 中的一条记录）。"""

    time: float = Field(..., ge=0, description="触发时间（秒）")
    note: int = Field(..., description="MIDI 音高编号（映射到 21 白键之一）")
    key: str = Field(..., description="映射到的键盘按键字母（Q/A/Z...）")
    action: str = Field(default="press", description="动作类型：press / release")
    duration: Optional[float] = Field(
        default=None, description="时值（秒），可选，用于试听与播放"
    )

    model_config = ConfigDict(extra="allow")


class KeyCoordinate(BaseModel):
    """单个琴键在屏幕上的中心坐标。"""

    key: str
    midi: int
    name: str
    x: float
    y: float


class Calibration(BaseModel):
    """四点校准结果。"""

    # 四角坐标，顺序：左上(TL)、右上(TR)、右下(BR)、左下(BL)
    corners: List[List[float]] = Field(..., description="四角坐标 [[x,y], ...]")
    # 21 个键中心坐标
    keys: List[KeyCoordinate] = Field(..., description="21 个键中心坐标")
    # 透视变换矩阵（3x3），可选
    perspective_matrix: Optional[List[List[float]]] = None
    # 截图尺寸 [w, h]
    image_size: Optional[List[int]] = None
    # 弹奏方式：screen（屏幕校准 + 鼠标点击）/ keyboard（键盘映射 Q-U/A-J/Z-M + 键盘按键）
    # 老文件里没有这个字段，缺省即 screen，因此旧校准文件照常能用。
    input_mode: str = Field(
        default=INPUT_MODE_SCREEN,
        description="弹奏方式：screen / keyboard",
    )


class MappingConfig(BaseModel):
    """21 白键映射配置。"""

    lowest_midi: int = 60  # C4
    highest_midi: int = 95  # B6
    white_keys: List[dict] = Field(default_factory=list, description="21 白键定义")
