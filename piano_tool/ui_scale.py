"""界面缩放：DPI 感知 + 用户可调字号（持久保存）。

两件事放在一起，因为它们是同一个问题的两面：

1. **为什么点播放后字体突然变小**
   `piano_tool.inject.ensure_dpi_aware()` 会把进程从「DPI 不感知」切成
   「per-monitor 感知」。这个切换如果发生在窗口已创建之后（历史上就是
   「第一次注入按键 = 点播放」那一刻），Windows 会立刻按物理像素重绘窗口，
   Tk 之前被系统位图拉伸出来的「大字」瞬间回到 96dpi 的真实尺寸 —— 视觉上
   就是「点播放后字体变小」。所以它必须在 `Tk()` 创建之前调用（见
   `ensure_dpi_aware_early()`），并且由我们自己按真实 DPI 设好 `tk scaling`。

2. **字号可调 + 记住**
   `apply_scale()` 同时改 `tk scaling`（控件尺寸/内边距）与一组命名字体的
   字号（文字本身），再写入 `data/ui_prefs.json`；下次启动 `load_scale()`
   读回来。基线只记录一次，反复调整不会累积放大。
"""

from __future__ import annotations

import json
import logging
import tkinter as tk
from pathlib import Path
from typing import Dict, Optional

from tkinter import font as tkfont

logger = logging.getLogger(__name__)

# 可选的界面缩放档位（1.0 = 100%）
SCALE_CHOICES = (0.85, 1.0, 1.15, 1.3, 1.5, 1.75, 2.0)
SCALE_DEFAULT = 1.0
SCALE_MIN, SCALE_MAX = 0.7, 2.5

# 受影响的命名字体（ttk 控件、Text、菜单、标题等都挂在它们上面）
NAMED_FONTS = (
    "TkDefaultFont", "TkTextFont", "TkFixedFont", "TkMenuFont",
    "TkHeadingFont", "TkCaptionFont", "TkSmallCaptionFont",
    "TkIconFont", "TkTooltipFont",
)

# 当前生效的缩放比例：给「硬编码字号」的地方换算用（scaled()）
_CURRENT_SCALE = SCALE_DEFAULT

# 命名字体基线（首次读到时缓存，之后所有窗口都拿它乘比例）
_BASELINES: Dict[str, int] = {}


def prefs_path() -> Path:
    """偏好文件位置（跟着项目 data 目录走，换机器也只在项目内）。"""
    return Path(__file__).resolve().parent.parent / "data" / "ui_prefs.json"


def clamp_scale(value) -> float:
    """把任意输入收敛到可用档位区间；非数字返回默认值。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return SCALE_DEFAULT
    if v != v:  # NaN
        return SCALE_DEFAULT
    return max(SCALE_MIN, min(SCALE_MAX, v))


def load_scale(path: Optional[Path] = None) -> float:
    """读上次保存的缩放；文件缺失/损坏一律回落到默认，不影响启动。"""
    return clamp_scale(_read_prefs(path).get("font_scale"))


def save_scale(scale: float, path: Optional[Path] = None) -> bool:
    """保存缩放（保留文件里其它字段，比如窗口尺寸）。"""
    return _update_prefs({"font_scale": round(clamp_scale(scale), 3)}, path)


# ---------------------------------------------------------------------------
# 窗口几何：记住上次的大小/位置，默认值按屏幕自适应
# ---------------------------------------------------------------------------
# 各页控件的最小可用尺寸：比它更小就没法用了
MIN_WINDOW_W, MIN_WINDOW_H = 620, 480


def _read_prefs(path: Optional[Path] = None) -> dict:
    """读整个偏好文件；任何异常都当成空偏好。"""
    p = Path(path) if path else prefs_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _update_prefs(patch: dict, path: Optional[Path] = None) -> bool:
    """合并写入：只改 patch 里的键，别的字段原样保留。"""
    p = Path(path) if path else prefs_path()
    try:
        raw = _read_prefs(p)
        raw.update(patch)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        logger.warning("保存界面偏好失败：%s", p, exc_info=True)
        return False


def load_window(path: Optional[Path] = None) -> Optional[dict]:
    """读上次关闭时的窗口几何；没有/坏掉返回 None（调用方回落到自适应默认）。"""
    win = _read_prefs(path).get("window")
    if not isinstance(win, dict):
        return None
    try:
        w, h = int(win.get("w")), int(win.get("h"))
    except (TypeError, ValueError):
        return None
    if w < MIN_WINDOW_W or h < MIN_WINDOW_H:
        return None
    out = {"w": w, "h": h, "zoomed": bool(win.get("zoomed"))}
    for k in ("x", "y"):
        try:
            out[k] = int(win[k])
        except (KeyError, TypeError, ValueError):
            out[k] = None
    return out


def save_window(w=None, h=None, x=None, y=None, zoomed=None,
                path: Optional[Path] = None) -> bool:
    """保存窗口几何。

    传 None 的字段保持原值 —— 窗口最大化时只更新 `zoomed`，不拿最大化尺寸
    去覆盖「正常状态」下的大小，否则下次取消最大化会突然变成满屏。
    """
    win = _read_prefs(path).get("window")
    win = dict(win) if isinstance(win, dict) else {}
    for key, val in (("w", w), ("h", h), ("x", x), ("y", y)):
        if val is not None:
            win[key] = int(val)
    if zoomed is not None:
        win["zoomed"] = bool(zoomed)
    return _update_prefs({"window": win}, path)


def fit_window_to_screen(w: int, h: int, screen_w: int, screen_h: int,
                         ratio_w: float = 0.92, ratio_h: float = 0.90
                         ) -> "tuple[int, int]":
    """把窗口尺寸收敛到「屏幕能装下且不失可用」的范围。

    这一步就是「默认显示不全」的解药：以前固定 1200×820，遇到 1366×768 这类
    笔记本屏，高度直接超出屏幕，底部按钮和日志区就被切掉了。
    """
    max_w = max(MIN_WINDOW_W, int(screen_w * ratio_w))
    max_h = max(MIN_WINDOW_H, int(screen_h * ratio_h))
    return max(MIN_WINDOW_W, min(int(w), max_w)), max(MIN_WINDOW_H, min(int(h), max_h))


def clamp_position(x: int, y: int, w: int, h: int,
                   screen_w: int, screen_h: int) -> "tuple[int, int]":
    """保证窗口至少露在屏幕里（换显示器/分辨率变化后不至于跑到屏幕外）。"""
    keep_x, keep_y = 120, 60      # 至少留这么多在屏幕内，标题栏抓得到
    x = max(-(w - keep_x), min(int(x), screen_w - keep_x))
    y = max(0, min(int(y), screen_h - keep_y))
    return x, y


def default_window(screen_w: int, screen_h: int, scale: float = 1.0
                   ) -> "tuple[int, int]":
    """没有历史记录时的默认尺寸：按屏幕比例给足，绝不超过屏幕。"""
    return fit_window_to_screen(int(1280 * scale), int(880 * scale),
                                screen_w, screen_h)


def load_calib_screen(path: Optional[Path] = None) -> Optional[dict]:
    """读「四点校准锁定哪块屏」。

    只存本机（`data/ui_prefs.json`），**不进任何预设文件**：换台机器或换套屏幕布局
    这个选择就没有意义了，跟着预设流传只会误点。
    """
    raw = _read_prefs(path).get("calib_screen")
    return raw if isinstance(raw, dict) else None


def save_calib_screen(screen: Optional[dict], path: Optional[Path] = None) -> bool:
    """保存/清除「四点校准锁定哪块屏」（同样只在本机生效）。"""
    return _update_prefs({"calib_screen": screen}, path)


def ensure_dpi_aware_early() -> bool:
    """**必须在 Tk() 创建之前调用**：先声明 DPI 感知，再建窗口。

    这样播放时 inject 再调一次也不会「突然重绘变小」（重复设置是幂等的，
    Windows 会直接返回失败/无变化）。
    """
    try:
        from .inject import ensure_dpi_aware

        ensure_dpi_aware()
        return True
    except Exception:  # noqa: BLE001  非 Windows / 无注入后端时忽略
        return False


def screen_dpi(root: tk.Misc) -> float:
    """取当前屏幕 DPI（96 = 100%）；取不到按 96 处理。"""
    try:
        dpi = float(root.winfo_fpixels("1i"))
        return dpi if dpi > 0 else 96.0
    except Exception:  # noqa: BLE001
        return 96.0


def capture_baselines(root: tk.Misc) -> Dict[str, int]:
    """记录命名字体的原始字号（只记一次，之后都拿它乘比例）。

    必须在改 `tk scaling` **之前**读：scaling 一变，Tk 回报的字体点值也会跟着变，
    拿被污染的值当基线会导致「设了 150% 却没变大」。
    结果缓存在模块级，同一进程里重建窗口也复用第一次的基线。
    """
    global _BASELINES
    if _BASELINES:
        return dict(_BASELINES)
    out: Dict[str, int] = {}
    for name in NAMED_FONTS:
        try:
            out[name] = int(tkfont.nametofont(name, root=root).actual("size"))
        except Exception:  # noqa: BLE001  个别字体在某些主题下不存在
            continue
    if out:
        _BASELINES = dict(out)
    return out


def apply_scale(root: tk.Misc, scale: float,
                baselines: Optional[Dict[str, int]] = None
                ) -> Dict[str, int]:
    """把缩放应用到整个界面，返回（首次调用时记录的）字体基线。

    - 命名字体的 size 负责文字本身（正数字号是「点」，不受 tk scaling 影响）；
    - `tk scaling` 负责控件尺寸/内边距。
    顺序很重要：先读基线，再改 scaling。
    """
    global _CURRENT_SCALE
    s = clamp_scale(scale)
    _CURRENT_SCALE = s

    if baselines is None:
        baselines = capture_baselines(root)      # 必须在改 scaling 之前

    dpi = screen_dpi(root)
    try:
        root.tk.call("tk", "scaling", (dpi / 72.0) * s)
    except Exception:  # noqa: BLE001
        pass

    for name, base in baselines.items():
        try:
            f = tkfont.nametofont(name, root=root)
            # base 为负表示「像素单位」，乘完保持负号即可
            f.configure(size=int(round(base * s)))
        except Exception:  # noqa: BLE001
            continue
    return baselines


def scaled(size: int) -> int:
    """把硬编码字号换算成当前缩放下的字号（给 canvas 文字、Text 等用）。"""
    return max(6, int(round(size * _CURRENT_SCALE)))


def current_scale() -> float:
    return _CURRENT_SCALE


def describe(scale: float) -> str:
    """'125%' 这种展示文案。"""
    return f"{int(round(clamp_scale(scale) * 100))}%"
