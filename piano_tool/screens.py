"""显示器枚举：给「四点校准」锁定一块演奏屏用。

**为什么需要它（双屏真实 bug）**

以前 ③ 页截屏用 `mss.shot()` 默认参数 = 截「所有显示器拼接成的一张大图」，
校准把**截图坐标**存进 `calibration.json`；播放时 `inject` 又按**虚拟桌面坐标**注入
（会减一次虚拟屏原点）。两者只在虚拟屏原点是 (0,0) 时相等：

- 副屏在主屏**右边/下边** → 原点 (0,0) → 坐标一致，能用；
- 副屏在主屏**左边/上边** → 原点变负（如 -1920）→ 截图坐标先被加 1920，播放时又被减一次，
  累计偏 3840，实测主屏上的键 (500,900) 会点到 (1919,900)，完全弹不中。

锁定一块屏之后：**只截这块屏**，坐标从这块屏的左上角算起，播放时加上它的物理原点即可，
与副屏怎么摆、有几块屏无关。

注意：这个选择只对本机有意义，**不写入任何预设文件**（存 `data/ui_prefs.json`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Windows GetSystemMetrics 的索引
SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


@dataclass(frozen=True)
class ScreenInfo:
    """一块显示器（坐标一律是物理像素）。"""

    index: int          # mss.monitors 的下标（1 起；0 是「全部拼接」，不用）
    left: int
    top: int
    width: int
    height: int
    is_primary: bool = False

    @property
    def origin(self) -> Tuple[int, int]:
        return self.left, self.top

    @property
    def size(self) -> Tuple[int, int]:
        return self.width, self.height

    @property
    def label(self) -> str:
        """下拉框里显示的文字。"""
        parts = [f"显示器 {self.index}", f"{self.width}×{self.height}"]
        if self.is_primary:
            parts.append("主屏")
        if self.left or self.top:
            parts.append(f"起点 {self.left},{self.top}")
        return " · ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index, "left": self.left, "top": self.top,
            "width": self.width, "height": self.height,
            "is_primary": self.is_primary,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["ScreenInfo"]:
        if not isinstance(data, dict):
            return None
        try:
            info = cls(
                index=int(data.get("index", 1)),
                left=int(data.get("left", 0)), top=int(data.get("top", 0)),
                width=int(data.get("width", 0)), height=int(data.get("height", 0)),
                is_primary=bool(data.get("is_primary", False)),
            )
        except (TypeError, ValueError):
            return None
        if info.width <= 0 or info.height <= 0:
            return None
        return info


def parse_monitors(monitors: Iterable[dict], primary_size: Tuple[int, int]
                   ) -> List[ScreenInfo]:
    """把 mss 的 `monitors` 列表转成 `ScreenInfo`（纯函数，便于测试）。

    `monitors[0]` 是「所有显示器拼接」，跳过；其余按出现顺序编号 1..n。
    主屏用「尺寸与 SM_CXSCREEN/SM_CYSCREEN 一致且原点为 (0,0)」来判定。
    """
    out: List[ScreenInfo] = []
    pw, ph = int(primary_size[0]), int(primary_size[1])
    for idx, m in enumerate(list(monitors)[1:], 1):
        try:
            info = ScreenInfo(
                index=idx,
                left=int(m["left"]), top=int(m["top"]),
                width=int(m["width"]), height=int(m["height"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if info.width <= 0 or info.height <= 0:
            continue
        is_primary = (info.left == 0 and info.top == 0
                      and info.width == pw and info.height == ph)
        out.append(ScreenInfo(index=info.index, left=info.left, top=info.top,
                              width=info.width, height=info.height,
                              is_primary=is_primary))
    # 兜底：一个都没认出主屏时，把原点 (0,0) 的那块（或第一块）当主屏
    if out and not any(s.is_primary for s in out):
        pick = next((s for s in out if s.left == 0 and s.top == 0), out[0])
        out = [ScreenInfo(s.index, s.left, s.top, s.width, s.height,
                          is_primary=(s.index == pick.index)) for s in out]
    return out


def primary_size() -> Tuple[int, int]:
    """主屏尺寸（物理像素）。"""
    try:
        import ctypes

        u = ctypes.windll.user32
        return int(u.GetSystemMetrics(SM_CXSCREEN)), int(u.GetSystemMetrics(SM_CYSCREEN))
    except Exception:  # noqa: BLE001  非 Windows
        return 1920, 1080


def virtual_screen_rect() -> Tuple[int, int, int, int]:
    """所有显示器联合的虚拟桌面 (x, y, w, h)，物理像素。

    坐标校验要用它：多屏时 x/y 可能为负，用主屏尺寸去卡会把副屏坐标误判成越界。
    """
    try:
        from .inject import ensure_dpi_aware

        ensure_dpi_aware()          # 不感知 DPI 时这里返回的是被折算过的逻辑尺寸
        import ctypes

        u = ctypes.windll.user32
        x = int(u.GetSystemMetrics(SM_XVIRTUALSCREEN))
        y = int(u.GetSystemMetrics(SM_YVIRTUALSCREEN))
        w = int(u.GetSystemMetrics(SM_CXVIRTUALSCREEN))
        h = int(u.GetSystemMetrics(SM_CYVIRTUALSCREEN))
        if w > 0 and h > 0:
            return x, y, w, h
        return 0, 0, int(u.GetSystemMetrics(SM_CXSCREEN)), int(u.GetSystemMetrics(SM_CYSCREEN))
    except Exception:  # noqa: BLE001
        return 0, 0, 1920, 1080


def list_screens() -> List[ScreenInfo]:
    """枚举当前所有显示器（每次调用都重新问系统，插拔显示器后要重调）。"""
    try:
        import mss

        with mss.mss() as sct:
            return parse_monitors(sct.monitors, primary_size())
    except Exception:  # noqa: BLE001  没有 mss / 非 Windows
        return []


def find_screen(screens: Iterable[ScreenInfo], saved: Optional[dict]
                ) -> Optional[ScreenInfo]:
    """按上次保存的记录找回那块屏。

    先按「原点和尺寸完全一致」找（最可靠），再退回「只有尺寸一致」，
    都找不到就返回 None（调用方回落到主屏）—— 换了显示器布局时不会乱点。
    """
    saved_info = ScreenInfo.from_dict(saved)
    if saved_info is None:
        return None
    pool = list(screens)
    for s in pool:
        if (s.left, s.top, s.width, s.height) == (
                saved_info.left, saved_info.top, saved_info.width, saved_info.height):
            return s
    for s in pool:
        if (s.width, s.height) == (saved_info.width, saved_info.height):
            return s
    return None


def pick_default(screens: Iterable[ScreenInfo]) -> Optional[ScreenInfo]:
    """默认选主屏；没有主屏标记就取第一块。"""
    pool = list(screens)
    if not pool:
        return None
    return next((s for s in pool if s.is_primary), pool[0])


def check_calibration_on_screen(calibration,
                               screen_rect: Optional[Tuple[int, int, int, int]] = None,
                               expect_size: Optional[Tuple[int, int]] = None
                               ) -> Tuple[bool, str]:
    """检查校准出的键坐标（换算成屏幕物理坐标后）是否落在虚拟桌面内。

    必须用**虚拟桌面**而不是主屏尺寸：多屏时副屏坐标可能是负数或超过主屏宽，
    拿主屏去卡会把正常的副屏坐标误判成越界（从而拦截播放）。

    `expect_size` = 当前演奏屏的（宽, 高）。给了就和校准时的截图尺寸比对：
    两边的像素基准不一致（换过显示器、改过系统缩放、老校准是 100% 缩放下截的）
    时坐标会整体错位，这种错误光看「是否越界」是发现不了的 —— 必须拦下来重做。
    """
    vx, vy, vw, vh = screen_rect if screen_rect else virtual_screen_rect()

    if expect_size and calibration.image_size:
        try:
            cal_w, cal_h = int(calibration.image_size[0]), int(calibration.image_size[1])
            exp_w, exp_h = int(expect_size[0]), int(expect_size[1])
        except (TypeError, ValueError, IndexError):
            cal_w = cal_h = exp_w = exp_h = 0
        if cal_w and cal_h and (abs(cal_w - exp_w) > 2 or abs(cal_h - exp_h) > 2):
            return False, (
                f"这份校准是在 {cal_w}×{cal_h} 下做的，当前演奏屏是 {exp_w}×{exp_h}"
                " —— 像素基准不同，点击会整体错位（常见于换过显示器、改过系统缩放/分辨率，"
                "或旧版本在 DPI 不感知时截的图）。请点「① 截取屏幕」重新校准。"
            )

    ox, oy = calibration.origin()
    pts = [(k.key, *calibration.screen_point(k)) for k in calibration.keys]
    if not pts:
        return True, "校准里没有键坐标"
    out = [key for key, x, y in pts
           if not (vx <= x < vx + vw and vy <= y < vy + vh)]
    xs = [x for _, x, _ in pts]
    ys = [y for _, _, y in pts]
    if out:
        return False, (
            f"有 {len(out)} 个键坐标超出屏幕（虚拟屏 {vw}×{vh} @{vx},{vy}，坐标范围 "
            f"x {min(xs):.0f}~{max(xs):.0f}, y {min(ys):.0f}~{max(ys):.0f}、越界键 {out}）。"
            "请重新做「③ 四点校准」（先选中模拟器所在的那块显示器再截屏）。"
        )
    hint = ""
    if not calibration.has_screen_origin() and (vx != 0 or vy != 0):
        hint = ("　⚠ 这份校准没有记录来自哪块显示器，而当前是多屏且虚拟屏原点为负，"
                "点击可能整体偏移 —— 建议选好显示器后重新校准。")
    origin_note = f"，已加屏幕原点 {ox},{oy}" if (ox or oy) else ""
    return True, (f"{len(calibration.keys)} 键坐标均在屏幕内"
                  f"（x {min(xs):.0f}~{max(xs):.0f}, y {min(ys):.0f}~{max(ys):.0f}"
                  f"{origin_note}）{hint}")
