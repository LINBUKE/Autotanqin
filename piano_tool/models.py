"""风物之诗琴自动化工具 · 数据模型

所有模块共享的 Pydantic 数据模型。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    # 人声匹配后写入：这个音唱的是哪个字（一个汉字一个音）。旧文件没有这个字段。
    lyric: Optional[str] = Field(
        default=None, description="对应的歌词字（人声匹配后写入）"
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
    # 校准截图来自哪块屏的左上角（物理像素）。keys 里的 x/y 是「相对这块屏」的坐标，
    # 真正点击的位置 = x/y + screen_origin。老文件没有这个字段 → 视作 (0, 0)，
    # 与「以前截全部屏、主屏在最左上」的行为完全一致。
    screen_origin: Optional[List[int]] = Field(
        default=None,
        description="校准截图对应显示器的左上角物理坐标 [x, y]，缺省视作 [0, 0]",
    )
    # 仅用于诊断/提示（第几块屏），不参与坐标计算
    screen_index: Optional[int] = Field(
        default=None, description="校准截图对应的显示器序号（1 起，仅诊断用）",
    )

    @field_validator("screen_origin", mode="before")
    @classmethod
    def _clean_screen_origin(cls, value):
        """坏值一律当「没记录」：手改坏的 JSON 不该让整份校准加载失败。"""
        if value is None:
            return None
        try:
            vals = [int(v) for v in value]
        except (TypeError, ValueError):
            return None
        return vals if len(vals) == 2 else None

    def origin(self) -> Tuple[int, int]:
        """校准坐标的屏幕原点（老文件/坏值都回落到 (0, 0)）。"""
        raw = self.screen_origin or (0, 0)
        try:
            return int(raw[0]), int(raw[1])
        except (TypeError, ValueError, IndexError):
            return 0, 0

    def screen_point(self, key: "KeyCoordinate") -> Tuple[float, float]:
        """把一个键的校准坐标换算成**屏幕物理坐标**（注入/点击都用这个）。"""
        ox, oy = self.origin()
        return float(key.x) + ox, float(key.y) + oy

    def has_screen_origin(self) -> bool:
        """老校准文件没有记录显示器（双屏时可能整体偏移）。"""
        return self.screen_origin is not None


class MappingConfig(BaseModel):
    """21 白键映射配置。"""

    lowest_midi: int = 60  # C4
    highest_midi: int = 95  # B6
    white_keys: List[dict] = Field(default_factory=list, description="21 白键定义")
