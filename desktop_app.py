"""风物之诗琴自动化工具 · 桌面版 GUI（Tkinter）

一个 Python 桌面软件，四步向导：
    ① 音频 → MIDI（basic_pitch 转写，后台线程）
    ② MIDI → 白键琴谱（events.json；音域/键位按② 页选的乐器）
    ③ 四点校准：截取屏幕 → 在截图上拖动 4 个角点对准琴键区四角
       → 透视变换自动算出各键中心坐标
    ④ 模拟点击播放（支持试运行 / 倍速 / 停止）

仅针对浏览器版模拟器，不连接游戏本体。

启动（务必用项目 venv，里面有 basic_pitch/tensorflow）：
    .venv\\Scripts\\python.exe desktop_app.py
或直接双击 启动.bat
"""
from __future__ import annotations

# ----------------------------------------------------------------------------
# 自动进入项目 venv（如果存在）
# 用户直接双击 desktop_app.py 或用系统 Python 运行时，会自动切到项目
# .venv 中的解释器，避免 "未检测到 basic_pitch" 的提示。
# ----------------------------------------------------------------------------
import os
import re
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_VENV_PY = _SCRIPT_DIR / ".venv" / "Scripts" / "python.exe"
if _VENV_PY.exists() and Path(sys.executable).resolve() != _VENV_PY.resolve():
    # 用项目 venv 重新启动本脚本，保持原有命令行参数
    args = [str(_VENV_PY), str(_SCRIPT_DIR / "desktop_app.py")] + sys.argv[1:]
    os.execv(str(_VENV_PY), args)
    # os.execv 成功则不会返回；若失败则继续用当前解释器执行，
    # 后续 basic_pitch 检测会给用户友好提示。

# 保证项目根目录在 sys.path 中，支持从其他工作目录启动
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import json
import logging
import queue
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from piano_tool import instruments
from piano_tool import backing_track
from piano_tool import preset
from piano_tool import sample_bank
from piano_tool import ui_scale
from piano_tool.config import get_config
from piano_tool.midi_mapper import fmt_range
from piano_tool.playclock import PlayClock

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("piano_tool.desktop")

C = get_config()

CORNER_LABELS = ("左上", "右上", "右下", "左下")


# ----------------------------------------------------------------------------
# 纯逻辑辅助函数（不依赖 Tk，可单测）
# ----------------------------------------------------------------------------
def place_default_corners(w: int, h: int) -> List[List[float]]:
    """给一张 w×h 的截图放置 4 个默认角点（TL, TR, BR, BL）。"""
    return [
        [w * 0.30, h * 0.30],
        [w * 0.70, h * 0.30],
        [w * 0.70, h * 0.70],
        [w * 0.30, h * 0.70],
    ]


def find_nearest_corner(
    corners: List[List[float]], x: float, y: float, radius: float
) -> Optional[int]:
    """返回距 (x, y) 在 radius 内的最近角点下标；没有则 None。"""
    best_i, best_d = None, None
    for i, (cx, cy) in enumerate(corners):
        d = ((cx - x) ** 2 + (cy - y) ** 2) ** 0.5
        if best_d is None or d < best_d:
            best_i, best_d = i, d
    if best_i is not None and best_d <= radius:
        return best_i
    return None


# ----------------------------------------------------------------------------
# 截屏预览图的安全创建（规避 Tk_GetPixmap: Error from CreateDIBSection）
# ----------------------------------------------------------------------------
# Windows 上 Tk 给 PhotoImage 分配位图时调用 CreateDIBSection；图片过大、
# 或进程内存吃紧时会直接失败，弹出「Tk_GetPixmap: Error from
# CreateDIBSection / 内存资源不足，无法处理此命令」，截屏流程中断。
# 本项目 venv 常驻 torch/demucs/basic_pitch，转写或人声分离时会占用
# 数 GB 内存，此时再分配几十 MB 的预览位图极易触发该错误。
# 对策：① 预览图尺寸硬上限；② 创建失败时自动逐级减半重试。
_MAX_PREVIEW_SIDE = 4096         # 预览图最长边上限（像素）
_MAX_PREVIEW_PIXELS = 8_000_000  # 预览图总像素上限（约 32MB 位图）


def safe_photo_from_bgr(disp):
    """把 BGR 截图转成 tk.PhotoImage，返回 (photo, 宽, 高)。

    先按 _MAX_PREVIEW_SIDE / _MAX_PREVIEW_PIXELS 硬上限缩小；
    若 tk.PhotoImage 仍因内存不足或位图过大失败（TclError:
    CreateDIBSection），自动减半重试，最多 8 次。
    """
    import cv2

    from piano_tool.imio import imwrite_unicode

    h, w = disp.shape[:2]
    cap = min(1.0,
              _MAX_PREVIEW_SIDE / float(max(w, h)),
              _MAX_PREVIEW_PIXELS / float(max(1, w * h)))
    if cap < 1.0:
        disp = cv2.resize(disp, (max(1, int(w * cap)), max(1, int(h * cap))),
                          interpolation=cv2.INTER_AREA)

    last_exc = None
    for _ in range(8):
        ph, pw = disp.shape[:2]
        if pw < 1 or ph < 1:
            break
        preview = C.data_dir / "screenshot_preview.png"
        try:
            if not imwrite_unicode(preview, disp):
                raise RuntimeError("无法写入预览图片（数据目录不可写？）")
            photo = tk.PhotoImage(file=str(preview))
            return photo, photo.width(), photo.height()
        except (tk.TclError, MemoryError) as exc:
            # 典型报错：Tk_GetPixmap: Error from CreateDIBSection
            last_exc = exc
            logger.warning("预览图创建失败，自动缩小一半后重试：%s", exc)
            if pw <= 2 and ph <= 2:
                break
            disp = cv2.resize(disp, (max(1, pw // 2), max(1, ph // 2)),
                              interpolation=cv2.INTER_AREA)
    raise RuntimeError(
        f"预览图创建失败：{last_exc}。\n"
        "这通常是图片过大或系统内存不足（本工具加载了 AI 模型，很吃内存）。\n"
        "请关闭其他占内存的程序，或重启本工具后再试一次。")


def soften_hint_text(ceiling_label: str, max_leap_label: str) -> str:
    """用大白话说明当前「高音柔化」参数会有什么效果。

    参数就是界面上两个下拉框的显示文本（如 "C6(84)" / "12" / "不限"）。
    结论来自真实谱子的实测，因此提示里是有依据的具体数字，不是套话。
    """
    c, leap = ceiling_label, max_leap_label
    # 提示里只说音名（"G5"），不带 "(79)" 这种 MIDI 数字，新手更好认；
    # 但在需要告诉用户「去选哪一项」时仍保留下拉框里的完整文本。
    cname = c.split("(")[0] if "(" in c else c
    if c == "不限" and leap == "不限":
        tip = "两个都选「不限」= 不做任何柔化，琴谱保持原样。"
    elif c == "不限":
        tip = ("不限幅、只把跳得太远的音挪近。注意：没有上限约束时它可能把音往上推，"
               "高音反而变多（实测 13.8% → 15.9%），建议配合限幅一起用。")
    elif leap == "不限":
        tip = (f"只把高于 {cname} 的音降八度，不处理跳进。想让旋律更连贯，"
               "就把「最大跳进」设成 12。")
    elif leap in ("9", "7"):
        tip = (f"最大跳进 {leap} 半音偏严格，为了凑近会把一些音往上推，"
               "高音反而可能变多，一般不建议。")
    elif c == "C6(84)":
        tip = ("推荐先用这个：只把偶尔飙高的音降下来，并把超过一个八度的硬跳变柔和。"
               "音名和曲调完全不变，整体音高也不变低。"
               "听着还是偏高偏硬，再把限幅改成 A5(81)。")
    elif c == "A5(81)":
        tip = ("高音基本都会下移一个八度，整体更柔和、也偏低一点（中位音高 E5 → C5）。"
               "如果听着发闷，就调回 C6(84)。")
    else:
        tip = (f"把高于 {cname} 的音降八度，并把超过 {leap} 半音的跳进挪到最近的八度。"
               "只是换个八度，音名和曲调都不变，可以放心试。")
    return "提示：" + tip


def inject_events_into_editor(html: str, events_json_text: str) -> str:
    """把 events 数据注入编辑器 HTML 副本，使其打开即载入琴谱。

    桌面版不走 HTTP，editor.html 里的 ?events= fetch 在 file:// 下会被
    浏览器拦截，因此直接把数据内联进页面。
    """
    payload = (
        "<script>window.addEventListener('load',function(){"
        "try{loadNotesFromArray(" + events_json_text + ")}"
        "catch(e){console.error('inject events failed',e);}});</script>"
    )
    marker = "</body>"
    if marker in html:
        return html.replace(marker, payload + marker, 1)
    return html + payload


def _build_events_json_from(data) -> list:
    """浏览器编辑器「保存」回传的数据 → events.json 结构（保留时值）。

    与 webui 的 build_events_json_from 不同，这里同时保留 duration，
    否则「按住时值」播放会丢失长短信息。
    """
    out = []
    for n in data:
        dur = n.get("duration")
        out.append({
            "time": round(float(n.get("time", 0)), 3),
            "note": int(n.get("note", 60)),
            "key": str(n.get("key", "")),
            "action": str(n.get("action", "press")),
            "duration": None if dur in (None, "") else round(float(dur), 3),
        })
    return out


def calibration_for_input_mode(input_mode: str):
    """播放时按弹奏方式取校准。

    **键盘方式（「原神键位优化」）绝不读 data/calibration.json**：它用坐标全 0 的
    占位校准，压根不需要屏幕坐标。而全新安装的机器上没有这个文件（发布包的 data/
    是空的），无条件读会直接 `FileNotFoundError` 把播放干掉 —— 2026-10-30 发布包
    第一版就踩了这个：用户勾了键盘优化，点播放还是报找不到 calibration.json。

    鼠标方式仍需真校准，文件不存在就让 load_calibration 抛异常，由调用方提示
    「尚未校准，请先完成③ 四点校准」。
    """
    if str(input_mode) == "keyboard":
        from piano_tool.keyboard_play import build_keyboard_calibration

        return build_keyboard_calibration()
    from piano_tool.player import load_calibration

    return load_calibration(C.calibration_path)


def _key_samples_info() -> dict:
    """列出 ④ 逐键音色采集顺手录下的每键音色（编辑器「模拟音」用）。"""
    d = Path(C.data_dir) / "key_samples"
    keys = sorted(p.stem for p in d.glob("*.wav")) if d.is_dir() else []
    info = {"ok": True, "keys": keys, "count": len(keys),
            "samplerate": 0, "sampled_at": None}
    idx = d / "index.json"
    if idx.is_file():
        try:
            meta = json.loads(idx.read_text(encoding="utf-8"))
            info["samplerate"] = int(meta.get("samplerate") or 0)
            info["sampled_at"] = meta.get("sampled_at")
        except Exception:  # noqa: BLE001  索引坏了不影响列文件
            pass
    return info


class _EditorHandler(BaseHTTPRequestHandler):
    """给「在浏览器微调」用的极简本地服务（仅 127.0.0.1）。

    让编辑器走 HTTP 而非 file://，从而：
      - 支持 ?events=/api/events 自动加载（fetch 不再被浏览器拦截）
      - 提供 POST /api/save-events，把微调结果直接写回 data/output/events.json
        闭环到播放（播放器每次都从磁盘重新读 events.json）
      - 提供 GET /api/ping，编辑器用它显示「已连接软件」PASS / FAIL
    """

    # 桌面版实例（`_start_editor_server` 里挂上），供 /api/ping 读播放状态
    app_ref = None

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/editor"):
            try:
                html = Path(C.editor_path).read_text(encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(exc)}))
                return
            self._send(200, html, "text/html; charset=utf-8")
            return
        if path == "/api/events":
            p = C.output_dir / "events.json"
            if not p.exists():
                self._send(404, json.dumps({"error": "尚未生成 events.json"}))
                return
            self._send(200, p.read_text(encoding="utf-8"),
                       "application/json; charset=utf-8")
            return
        if path == "/api/ping":
            # 编辑器的连接状态灯：能返回 200 就说明本地服务活着（= 软件在运行）
            info = {"ok": True, "app": "windsong", "playing": False, "events": 0}
            app = getattr(_EditorHandler, "app_ref", None)
            if app is not None:
                try:
                    info["playing"] = bool(getattr(app, "_playing", False))
                except Exception:  # noqa: BLE001
                    pass
            try:
                p = C.output_dir / "events.json"
                if p.exists():
                    info["events"] = len(json.loads(p.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001  谱子坏了不影响连通性判断
                pass
            self._send(200, json.dumps(info, ensure_ascii=False))
            return
        if path == "/api/play-state":
            # 编辑器的播放头跟随：位置由「开播时刻 + 已过时间 × 倍速」实时算，
            # 停止/暂停时冻结在那一刻（不会跳回开头）。
            state = {"playing": False, "position": 0.0, "total": 0.0, "speed": 1.0}
            app = getattr(_EditorHandler, "app_ref", None)
            if app is not None:
                try:
                    state = app.play_state_snapshot()
                except Exception:  # noqa: BLE001
                    pass
            self._send(200, json.dumps(state, ensure_ascii=False))
            return
        if path == "/api/samples":
            # 「模拟音」用：④ 逐键音色采集顺手录下的每键真实琴声
            info = _key_samples_info()
            self._send(200, json.dumps(info, ensure_ascii=False))
            return
        if path == "/api/sample":
            # 单个键的 wav（浏览器 decodeAudioData 直接用）
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            key = (qs.get("key") or [""])[0]
            name = re.sub(r"[^A-Za-z0-9_]", "", key)      # 只允许字母数字，挡路径穿越
            p = Path(C.data_dir) / "key_samples" / f"{name}.wav" if name else None
            if p is None or not p.is_file():
                self._send(404, json.dumps({"error": "没有这个键的模拟音"}))
                return
            try:
                self._send(200, p.read_bytes(), "audio/wav")
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(exc)}))
            return
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path == "/api/save-events":
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"[]"
                data = json.loads(raw.decode("utf-8"))
                # 新格式：{"events": [...], "instrument": "soprano"}；旧格式：直接是数组
                instrument = None
                if isinstance(data, dict):
                    instrument = data.get("instrument")
                    data = data.get("events")
                if not isinstance(data, list):
                    raise ValueError("顶层必须是数组")
            except Exception as exc:  # noqa: BLE001
                self._send(400, json.dumps({"error": f"解析失败：{exc}"}))
                return
            try:
                out = C.output_dir / "events.json"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(
                    json.dumps(_build_events_json_from(data), indent=2,
                               ensure_ascii=False),
                    encoding="utf-8",
                )
                # 记下这份谱子属于哪件乐器：不同乐器的键位字母不同，
                # 播放时按同一件乐器取键位表，字母才不会和音高错位。
                meta = C.output_dir / "events_instrument.json"
                if instrument:
                    meta.write_text(
                        json.dumps({"instrument": instrument},
                                   indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                elif meta.exists():
                    meta.unlink()
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": f"写入失败：{exc}"}))
                return
            self._send(200, json.dumps({"ok": True, "count": len(data),
                                        "instrument": instrument}))
            return
        self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, *args, **kwargs):  # noqa: D401
        pass  # 静默，避免刷屏


def basic_pitch_available() -> Tuple[bool, str]:
    """检查 basic_pitch 是否可用，返回 (是否可用, 提示信息)。"""
    try:
        import basic_pitch  # noqa: F401

        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def check_coords_on_screen(calibration, screen_rect: Optional[Tuple[int, int, int, int]] = None,
                           expect_size: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
    """校验校准坐标是否落在屏幕内（实现见 `piano_tool.screens`，便于单测）。"""
    from piano_tool.screens import check_calibration_on_screen

    return check_calibration_on_screen(calibration, screen_rect, expect_size)


# ----------------------------------------------------------------------------
# 瞄准镜选窗：全屏半透明覆盖层，瞄准任意窗口后单击/松手锁定
# ----------------------------------------------------------------------------
class _CrosshairPicker(tk.Toplevel):
    """全屏瞄准覆盖层：移动鼠标瞄准任意窗口，单击（或按住拖过去松手）即锁定。

    安全边界：锁定瞬间只通过 Win32 按屏幕坐标查询该窗口的句柄、标题与进程号，
    供焦点守卫使用。不截屏、不读窗口内容、不注入、不修改任何文件。
    """

    R = 30          # 准星圆环半径
    GAP = 12        # 十字延长线离圆环的距离
    LINE = 24       # 十字延长线长度

    def __init__(self, master, on_picked, on_cancel=None):
        super().__init__(master)
        self._on_picked = on_picked
        self._on_cancel = on_cancel
        self._done = False

        from piano_tool.focus_guard import cursor_pos, virtual_screen_rect

        self._vx, self._vy, self._vw, self._vh = virtual_screen_rect()

        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.38)
        self.configure(bg="#101014")
        self.geometry(f"{self._vw}x{self._vh}+{self._vx}+{self._vy}")

        self._cv = tk.Canvas(self, bg="#101014", highlightthickness=0,
                             cursor="crosshair")
        self._cv.pack(fill="both", expand=True)

        self._hint = "移动鼠标瞄准目标窗口，单击或松手锁定　·　Esc / 右键取消"
        self._sub = "仅读取窗口标题与进程号用于焦点守卫，不读取画面、不修改任何内容"
        self._draw_hint("#ffffff")

        cx, cy = cursor_pos()
        self._draw_cross(cx - self._vx, cy - self._vy)

        self._cv.bind("<Motion>", self._on_move)
        self._cv.bind("<ButtonRelease-1>", self._on_lock)
        self._cv.bind("<Button-3>", lambda _e: self._cancel())
        self.bind("<Escape>", lambda _e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def show(self):
        self.deiconify()
        self.lift()
        try:
            self.grab_set()
        except Exception:  # noqa: BLE001  个别窗口管理器不支持 grab
            pass
        self.focus_force()

    def _draw_hint(self, color):
        self._cv.delete("hint")
        self._cv.create_text(self._vw // 2, 40, text=self._hint, fill=color,
                             font=("Microsoft YaHei UI", ui_scale.scaled(18), "bold"), tags="hint")
        self._cv.create_text(self._vw // 2, 78, text=self._sub, fill="#d6dae2",
                             font=("Microsoft YaHei UI", ui_scale.scaled(11)), tags="hint")

    def _draw_cross(self, x, y):
        self._cv.delete("cross")
        yellow = "#ffd400"
        r, gap, ln = self.R, self.GAP, self.LINE
        self._cv.create_oval(x - r, y - r, x + r, y + r, outline=yellow,
                             width=3, tags="cross")
        self._cv.create_line(x - r - gap, y, x - r - gap - ln, y,
                             fill=yellow, width=3, tags="cross")
        self._cv.create_line(x + r + gap, y, x + r + gap + ln, y,
                             fill=yellow, width=3, tags="cross")
        self._cv.create_line(x, y - r - gap, x, y - r - gap - ln,
                             fill=yellow, width=3, tags="cross")
        self._cv.create_line(x, y + r + gap, x, y + r + gap + ln,
                             fill=yellow, width=3, tags="cross")
        self._cv.create_oval(x - 3, y - 3, x + 3, y + 3,
                             fill="#ff3b30", outline="", tags="cross")

    def _on_move(self, event):
        self._draw_cross(event.x, event.y)

    def _on_lock(self, event):
        if self._done:
            return
        from piano_tool.focus_guard import capture_window_at

        sx, sy = event.x_root, event.y_root
        # 覆盖层自己挡在最前，WindowFromPoint 会命中它 —— 先彻底隐藏再查
        try:
            self.grab_release()
        except Exception:  # noqa: BLE001
            pass
        self.withdraw()
        self.update_idletasks()
        self.update()
        time.sleep(0.06)  # 等窗口管理器真正完成隐藏

        target = capture_window_at(sx, sy)
        if target is None or not target.title.strip() or target.pid == os.getpid():
            # 没选中有效窗口（桌面/本工具自身/无标题区域）：恢复覆盖层让用户重瞄
            self._hint = ("没选中有效窗口（可能是桌面或本工具窗口），"
                          "请瞄准模拟器 / 目标应用的窗口后再松手")
            self._draw_hint("#ff6b6b")
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            try:
                self.grab_set()
            except Exception:  # noqa: BLE001
                pass
            self.focus_force()
            return

        self._done = True
        self.destroy()
        self._on_picked(target)

    def _cancel(self):
        if self._done:
            return
        self._done = True
        try:
            self.grab_release()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()
        if self._on_cancel:
            self._on_cancel()


# ----------------------------------------------------------------------------
# 主窗口
# ----------------------------------------------------------------------------
class WindsongApp(tk.Tk):
    def __init__(self):
        # 必须在 Tk() 创建之前声明 DPI 感知：否则「第一次注入按键（= 点播放）」时
        # 才切到 per-monitor aware，Windows 会立刻按物理像素重绘，字体视觉上突然变小。
        ui_scale.ensure_dpi_aware_early()
        super().__init__()

        # 界面缩放：先按真实 DPI 把界面撑到正确尺寸，再按上次保存的字号应用
        self._font_scale = ui_scale.load_scale()
        self._font_baselines = ui_scale.apply_scale(self, self._font_scale)

        self.title("风物之诗琴自动化工具 · 桌面版")
        self._restore_window_geometry()

        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._play_stop = threading.Event()
        self._play_thread: Optional[threading.Thread] = None
        self._busy = False
        self._playing = False          # 是否正在播放/自检（F7 切换用）
        self._play_paused = False      # 三连右键暂停中（再按三连继续）
        self._resume_position = 0.0    # 暂停时的音乐时刻（恢复从这里接着弹）
        self._last_play_args = None    # 本次播放的参数元组（恢复要沿用同一套）
        self._hotkey = None            # keyboard 库句柄
        self._gesture = None           # 播放中的三连右键监听器（gestures.py）
        # 播放时钟：给浏览器编辑器同步播放头用（见 piano_tool/playclock.py）
        self._play_clock = PlayClock()

        # 校准画布状态
        self._shot_bgr = None            # 全分辨率截图 (cv2 BGR)
        self._shot_scale = 1.0           # 显示缩放比例
        self._photo = None               # tk.PhotoImage（保持引用）
        self._corners: List[List[float]] = []  # 图像坐标 TL,TR,BR,BL
        self._drag_idx: Optional[int] = None
        self._cal: Optional[object] = None
        self._latency_lead: Dict[str, float] = {}   # 已停用（仅保留兼容；④ 采样用于采集编辑器音色）
        self._audio_devices: list = []             # 可用的音频输入设备
        self._dev_label_to_index: Dict[str, int] = {}  # 下拉框标签 -> 设备索引
        self._dev_label_loopback: Dict[str, bool] = {}  # 下拉框标签 -> 是否回环(系统声音)

        # 浏览器编辑器本地服务（仅 127.0.0.1，用于「在浏览器微调」闭环）
        self._editor_server = None            # ThreadingHTTPServer 实例
        self._editor_port: Optional[int] = None
        self._editor_lock = threading.Lock()

        # 「同一首歌」的人声轨登记：① 页勾选人声分离转写成功后，把
        # {产物 MIDI 绝对路径: 本次分离出的 vocals.wav} 记下来；② 页映射
        # 同一份 MIDI 时自动带上它做基频校准（只对首席女高音生效）。
        # 换 MIDI / 重新转写（含不带分离的）都会更新或清除，不会串歌。
        self._vocals_for_midi: Dict[str, str] = {}

        # ⑤ 页「同时播放伴奏」正在放的实例（`backing_track.BackingTrack`）。
        # 播放中改音量直接改它的 volume；停止/暂停时置回 None。
        self._backing = None

        self._bp_ok, self._bp_msg = basic_pitch_available()

        # 焦点守卫的目标窗口（⑤ 页「抓取当前窗口」写入，抓一次长期有效）
        try:
            from piano_tool.focus_guard import load_target

            self._focus_target = load_target()
        except Exception:  # noqa: BLE001  没有 pywin32 / 文件损坏都不该挡住启动
            self._focus_target = None

        self._build_ui()
        self.after(120, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # 上次是最大化状态的话，等窗口真正显示出来再恢复（Windows 上要 after_idle）
        if getattr(self, "_restore_zoomed", False):
            self.after_idle(self._apply_zoomed)
        self._setup_hotkey()
        # 沿用上次关闭时的 ②③④⑤ 设置（省得每次开软件都重新调一遍）
        self._load_session_preset()

        self.log("欢迎使用风物之诗琴自动化工具（桌面版）")
        if self._bp_ok:
            self.log("✓ basic_pitch 已就绪，可以开始「① 音频→MIDI」")
        else:
            self.log("✗ 未检测到 basic_pitch：" + self._bp_msg)
            self.log("  请通过项目目录下的 启动.bat 启动，或检查 .venv 是否已正确安装依赖。")

        # 启动器自提升的降级提示：launcher.ps1 申请管理员被拒（UAC 点了「否」）时会
        # 把 WINDSONG_NO_ELEVATION=1 传给本进程。命中说明本次没拿到管理员权限。
        if os.environ.get("WINDSONG_NO_ELEVATION"):
            self.log("⚠ 本次以普通权限运行（未获得管理员权限）。")
            self.log("  若目标程序是以管理员身份运行的，Windows 会静默丢弃本软件发给它的按键/点击，")
            self.log("  表现为「浏览器能弹、切到游戏没反应」。需要时请关闭软件后重新双击 启动.bat，")
            self.log("  并在 UAC 弹窗点「是」。可点⑤页「🩺 目标窗口体检」查看具体原因。")
        else:
            # 主动权限体检（不靠启动器也能发现问题）：保存的目标窗口是管理员权限、
            # 而本软件不是时，Windows 的 UIPI 会静默丢弃输入 —— 浏览器不受影响，
            # 表现为「浏览器能弹、切到游戏没反应」。直接双击 desktop_app.py 或从
            # IDE 启动时启动器没跑，这里补一次只读检查，给出可操作的提示。
            try:
                from piano_tool.focus_guard import process_elevation

                tgt_pid = int(getattr(self._focus_target, "pid", 0) or 0)
                if tgt_pid and process_elevation(tgt_pid) == "elevated" \
                        and process_elevation(os.getpid()) != "elevated":
                    self.log("⚠ 检测到目标窗口以【管理员权限】运行，而本软件是普通权限。")
                    self.log("  Windows 会静默丢弃本软件发给它的按键/点击（浏览器不受影响，")
                    self.log("  所以表现为「浏览器能弹、切到游戏没反应」）。")
                    self.log("  解决：关闭本软件，重新双击项目里的 启动.bat 并在 UAC 弹窗点「是」")
                    self.log("  （启动器会自动以管理员身份重启本软件）；或右键 启动.bat → 以管理员身份运行。")
                    self.log("  可点⑤页「🩺 目标窗口体检」查看完整结论。")
            except Exception:  # noqa: BLE001  读不到权限级别不该影响启动
                pass

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        # ---- 顶部字号条：每页都能调，改完立刻生效并自动记住 ----
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Label(bar, text="界面字号：").pack(side="left")
        self._var_font_scale = tk.StringVar(value=ui_scale.describe(self._font_scale))
        self._cb_font_scale = ttk.Combobox(
            bar, textvariable=self._var_font_scale, width=6, state="readonly",
            values=[ui_scale.describe(s) for s in ui_scale.SCALE_CHOICES])
        self._cb_font_scale.pack(side="left")
        self._cb_font_scale.bind("<<ComboboxSelected>>", self._on_font_scale)
        ttk.Button(bar, text="A−", width=3,
                   command=lambda: self._step_font(-1)).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="A＋", width=3,
                   command=lambda: self._step_font(1)).pack(side="left", padx=(2, 0))
        ttk.Label(bar, text="（自动保存）",
                  foreground="#666").pack(side="left", padx=(6, 0))
        self._lbl_font_scale = ttk.Label(bar, text="", foreground="#1a5fb4")
        self._lbl_font_scale.pack(side="right")

        # ---- 预设：把 ②③④⑤ 的设置打包成 .ini，换歌/换机不用一项项重调 ----
        ttk.Separator(bar, orient="vertical").pack(side="left", padx=10, fill="y")
        ttk.Label(bar, text="预设：").pack(side="left")
        ttk.Button(bar, text="导入…", width=7,
                   command=self._on_import_preset).pack(side="left", padx=2)
        ttk.Button(bar, text="导出设置…", width=11,
                   command=lambda: self._on_export_preset(False)).pack(side="left", padx=2)
        ttk.Button(bar, text="导出全量(含MIDI/采样)…", width=24,
                   command=lambda: self._on_export_preset(True)).pack(side="left", padx=2)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(4, 4))
        self._nb = nb
        nb.add(self._frame_convert(), text=" ① 音频 → MIDI ")
        nb.add(self._frame_map(), text=" ② MIDI → 琴谱 ")
        nb.add(self._frame_calibrate(), text=" ③ 四点校准 ")
        nb.add(self._frame_latency(), text=" ④ 逐键音色采集 ")
        nb.add(self._frame_play(), text=" ⑤ 播放 ")

        logf = ttk.LabelFrame(self, text=" 日志 ")
        logf.pack(fill="both", padx=8, pady=(0, 8))
        self.log_text = tk.Text(logf, height=8, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, side="left")
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        sb.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=sb.set)

    def _ui_dpi_ratio(self) -> float:
        """系统 DPI 比例（150% 缩放 = 1.5）。

        进程是 DPI 感知的，窗口尺寸按物理像素算；不乘这个比例的话，
        在 150% 缩放的屏上默认窗口会只有「逻辑 853 宽」，显得又小又挤。
        """
        try:
            return max(1.0, ui_scale.screen_dpi(self) / 96.0)
        except Exception:  # noqa: BLE001
            return 1.0

    def _restore_window_geometry(self):
        """恢复上次关闭时的窗口大小/位置；没有记录就按屏幕给一个装得下的默认。

        以前是写死 1200×820，遇到 1366×768 这类屏高度直接超出，底部按钮和日志
        被切掉 —— 所以默认值一律按屏幕比例收敛，绝不大于屏幕。
        """
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        saved = ui_scale.load_window()
        self._restore_zoomed = False
        if saved:
            # 允许比默认略大（用户自己拉过的尺寸要尊重），但仍不能超出屏幕
            w, h = ui_scale.fit_window_to_screen(saved["w"], saved["h"], sw, sh, 0.98, 0.96)
            if saved.get("x") is None or saved.get("y") is None:
                x, y = (sw - w) // 2, max(0, (sh - h) // 3)
            else:
                x, y = ui_scale.clamp_position(saved["x"], saved["y"], w, h, sw, sh)
            self.geometry(f"{w}x{h}+{x}+{y}")
            self._restore_zoomed = bool(saved.get("zoomed"))
        else:
            # 默认尺寸 = 设计尺寸 × DPI 比例 × 字号比例
            w, h = ui_scale.default_window(
                sw, sh, self._font_scale * self._ui_dpi_ratio())
            self.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 3)}")
        # 最小尺寸同样不能超过屏幕，否则小屏上根本缩不小
        self.minsize(min(ui_scale.MIN_WINDOW_W, int(sw * 0.8)),
                     min(ui_scale.MIN_WINDOW_H, int(sh * 0.7)))

    def _apply_zoomed(self):
        """恢复最大化状态（Windows 用 'zoomed'，别的平台忽略失败即可）。"""
        try:
            if str(self.state()) != "zoomed":
                self.state("zoomed")
        except Exception:  # noqa: BLE001
            pass

    def _save_window_geometry(self):
        """关闭时记住窗口几何。

        最大化状态只记 zoomed 标记：此时 geometry() 返回的是最大化后的尺寸，
        拿它覆盖「正常大小」会导致下次取消最大化变成满屏。
        """
        try:
            zoomed = str(self.state()) == "zoomed"
        except Exception:  # noqa: BLE001  个别窗口管理器不支持 state
            zoomed = False
        if zoomed:
            ui_scale.save_window(zoomed=True)
            return
        try:
            m = re.match(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", str(self.geometry()))
            if not m:
                return
            w, h, x, y = (int(m.group(i)) for i in (1, 2, 3, 4))
            ui_scale.save_window(w=w, h=h, x=x, y=y, zoomed=False)
        except Exception:  # noqa: BLE001
            logger.warning("保存窗口尺寸失败", exc_info=True)

    def _apply_font_scale(self, scale, initial=False):
        """应用界面字号：立刻生效 + 写入偏好 + 必要时把窗口撑大。"""
        s = ui_scale.clamp_scale(scale)
        ui_scale.apply_scale(self, s, self._font_baselines)
        self._font_scale = s
        ui_scale.save_scale(s)
        if hasattr(self, "_var_font_scale"):
            self._var_font_scale.set(ui_scale.describe(s))
        # 字号变大后内容需要更多空间：只放大不缩小，别覆盖用户手动调过的窗口尺寸
        if not initial:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            need_w, need_h = ui_scale.default_window(
                sw, sh, s * self._ui_dpi_ratio())
            cur_w, cur_h = self.winfo_width(), self.winfo_height()
            if cur_w < need_w or cur_h < need_h:
                nw, nh = ui_scale.fit_window_to_screen(
                    max(cur_w, need_w), max(cur_h, need_h), sw, sh, 0.98, 0.96)
                self.geometry(f"{nw}x{nh}")
        if hasattr(self, "_lbl_font_scale"):
            self._lbl_font_scale.configure(text=f"当前 {ui_scale.describe(s)}（已保存）")
        if hasattr(self, "log_text"):
            self.log(f"界面字号已调为 {ui_scale.describe(s)}（已记住，下次启动沿用）")

    def _on_font_scale(self, _evt=None):
        raw = str(self._var_font_scale.get()).replace("%", "").strip()
        try:
            pct = float(raw)
        except ValueError:
            pct = 100.0
        self._apply_font_scale(pct / 100.0)

    def _step_font(self, step: int):
        choices = list(ui_scale.SCALE_CHOICES)
        idx = min(range(len(choices)), key=lambda i: abs(choices[i] - self._font_scale))
        idx = max(0, min(len(choices) - 1, idx + step))
        self._apply_font_scale(choices[idx])

    # ---------------- 用户预设：②③④⑤ 打包成 .ini，可导出/导入/关掉重开沿用 ----------------
    def _preset_state(self, with_song: bool = False) -> dict:
        """把 ②③④⑤ 的当前设置打包成预设结构（对应 .ini 的各段）。"""

        def read_json(p):
            try:
                return json.loads(Path(p).read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001  没有/损坏都当不存在
                return None

        state: dict = {
            "meta": preset.build_meta(with_song),
            "app": {"font_scale": round(self._font_scale, 3)},
            "instrument": {"name": self._var_instrument.get()},
            "map": {
                "autotp": int(bool(self._var_autotp.get())),
                "mono": int(bool(self._var_mono.get())),
                "contour": int(bool(self._var_contour.get())),
                "melody_mode": self._var_melody_mode.get(),
                "oor": self._var_oor.get(),
                "arrange": self._var_arrange.get(),
                "polyphony": self._var_polyphony.get(),
                "mindur": self._var_mindur.get(),
                "ceiling": self._var_ceiling.get(),
                "maxleap": self._var_maxleap.get(),
                "grid": self._var_grid.get(),
                "bpm": self._var_bpm.get(),
            },
            "play": {
                "dry": int(bool(self._var_dry.get())),
                "upper": int(bool(self._var_upper.get())),
                "speed": self._var_speed.get(),
                "delay": self._var_delay.get(),
                "countdown": self._var_countdown.get(),
                "latency": self._var_latency.get(),
                "hold": int(bool(self._var_hold.get())),
                "holdmin": self._var_holdmin.get(),
                "backing": int(bool(self._var_backing.get())),
                "backing_vol": self._var_backing_vol.get(),
                "backing_src": self._var_backing_src.get(),
                "focus_guard": int(bool(self._var_focus_guard.get())),
                "focus_word": self._var_focus_word.get(),
                "focus_lost": self._var_focus_lost.get(),
            },
            "latency": {
                "manual_ms": self._var_manual_latency.get(),
                "keyopt": int(bool(self._var_keyopt.get())),
            },
        }
        cal = read_json(C.calibration_path)
        if cal is not None:
            state["calibration"] = {"payload": preset.pack_blob(cal)}
        lat_prof = read_json(C.latency_profile_path)
        if lat_prof is not None:
            state["latency"]["payload"] = preset.pack_blob(lat_prof)
        try:
            from piano_tool.focus_guard import TARGET_PATH

            ft = read_json(TARGET_PATH)
            if ft is not None:
                state["focus_target"] = {"payload": preset.pack_blob(ft)}
        except Exception:  # noqa: BLE001  没有 pywin32 之类，忽略
            pass
        if with_song:
            midi = Path(str(self._var_midi.get()).strip() or "")
            if midi.is_file():
                state["song"] = {"midi_name": midi.name,
                                 "midi_b64": preset.b64_of(midi.read_bytes())}
            # ④ 采样顺手录下的每键音色（编辑器「模拟音」用）：全量预设一起带走，
            # 换机器 / 换新发布包时导入就恢复，不用对着游戏再录一遍。
            # 单独 try：采样读失败不该把整份预设导出搞崩。
            self._sample_note = ""
            try:
                sec, n, total, note = sample_bank.build_section(
                    Path(C.data_dir) / "key_samples")
                self._sample_note = note
                if sec:
                    state["samples"] = sec
            except Exception:  # noqa: BLE001
                logger.warning("打包每键采样失败（预设照常导出，只是不带采样）", exc_info=True)
                self._sample_note = "每键采样打包失败，这次没带（详见日志）"
        return state

    def _apply_preset_state(self, state: dict) -> list:
        """把预设写进界面与磁盘，返回「改了哪些东西」给日志用。"""
        done: list = []

        # 1) 界面字号
        raw_fs = state.get("app", {}).get("font_scale")
        if raw_fs is not None:
            try:
                fs = ui_scale.clamp_scale(float(raw_fs))
                if abs(fs - self._font_scale) > 1e-6:
                    self._apply_font_scale(fs)
                    done.append(f"界面字号 {ui_scale.describe(fs)}")
            except (TypeError, ValueError):
                pass

        # 2) 乐器：必须先切——切乐器会把 ② 参数重置成该乐器的推荐值
        name = str(state.get("instrument", {}).get("name", "")).strip()
        if name:
            inst = instruments.get_instrument(name)
            if inst.name != self._var_instrument.get():
                self._var_instrument.set(inst.name)
                self._on_instrument_change()
                done.append(f"乐器 {inst.name}")

        # 3) ② MIDI→琴谱参数
        m = state.get("map", {})
        touched = False
        for key, attr in (("autotp", "_var_autotp"), ("mono", "_var_mono"),
                          ("contour", "_var_contour")):
            if key in m:
                getattr(self, attr).set(preset.to_bool(m[key]))
                touched = True
        for key, attr in (("melody_mode", "_var_melody_mode"), ("oor", "_var_oor"),
                          ("arrange", "_var_arrange"), ("polyphony", "_var_polyphony"),
                          ("ceiling", "_var_ceiling"), ("maxleap", "_var_maxleap"),
                          ("grid", "_var_grid")):
            if key in m:
                getattr(self, attr).set(str(m[key]))
                touched = True
        for key, attr in (("mindur", "_var_mindur"), ("bpm", "_var_bpm")):
            if key in m:
                try:
                    getattr(self, attr).set(float(m[key]))
                    touched = True
                except (TypeError, ValueError):
                    pass
        if touched:
            done.append("② 映射参数")
            try:
                self._on_arrange_changed()
            except Exception:  # noqa: BLE001
                pass
            # 程序改 StringVar 不会触发 Combobox 的 <<ComboboxSelected>>，
            # 这里补一次刷新，否则导入后「柔化」说明还是旧参数。
            try:
                self._update_soften_hint()
            except Exception:  # noqa: BLE001
                pass

        # 4) ④ 逐键音色采集（界面部分）
        lat = state.get("latency", {})
        touched = False
        if "manual_ms" in lat:
            try:
                self._var_manual_latency.set(int(float(lat["manual_ms"])))
                touched = True
            except (TypeError, ValueError):
                pass
        if "keyopt" in lat:
            self._var_keyopt.set(preset.to_bool(lat["keyopt"]))
            touched = True
            # 勾选框的 command 只在用户手点时触发；导入/沿用上次设置时
            # 必须手动刷新③页那段「当前：键盘弹奏 / 老方案」说明。
            try:
                self._update_keyopt_hint()
            except Exception:  # noqa: BLE001
                pass
        if touched:
            done.append("④ 音色采集设置")

        # 5) ⑤ 播放参数
        p = state.get("play", {})
        touched = False
        for key, attr in (("dry", "_var_dry"), ("upper", "_var_upper"),
                          ("hold", "_var_hold"), ("focus_guard", "_var_focus_guard"),
                          ("backing", "_var_backing")):
            if key in p:
                getattr(self, attr).set(preset.to_bool(p[key]))
                touched = True
        for key, attr, conv in (("speed", "_var_speed", float),
                                ("delay", "_var_delay", int),
                                ("countdown", "_var_countdown", int),
                                ("latency", "_var_latency", int),
                                ("holdmin", "_var_holdmin", int),
                                ("backing_vol", "_var_backing_vol", int)):
            if key in p:
                try:
                    getattr(self, attr).set(conv(float(p[key])))
                    touched = True
                except (TypeError, ValueError):
                    pass
        for key, attr in (("focus_word", "_var_focus_word"),
                          ("focus_lost", "_var_focus_lost"),
                          ("backing_src", "_var_backing_src")):
            if key in p:
                val = str(p[key])
                # 伴奏来源存的是下拉框文案；旧预设/手改的值也要能落到两个选项之一
                if key == "backing_src":
                    val = backing_track.SOURCE_LABELS[
                        backing_track.normalize_source(val)]
                getattr(self, attr).set(val)
                touched = True
        if touched:
            done.append("⑤ 播放参数")
            # 这些控件也用 command 刷新说明，程序改值不会自己触发：
            # 「试运行/真实点击」状态灯、焦点守卫说明都要补刷。
            for fn in ("_on_toggle_dry", "_update_focus_hint"):
                try:
                    getattr(self, fn)()
                except Exception:  # noqa: BLE001
                    pass

        # 6) ③ 四点校准（写回校准文件）
        blob = state.get("calibration", {}).get("payload")
        cal = preset.unpack_blob(blob) if blob else None
        if isinstance(cal, dict) and cal:
            try:
                Path(C.calibration_path).write_text(
                    json.dumps(cal, indent=2, ensure_ascii=False), encoding="utf-8")
                n = len(cal.get("keys") or [])
                mode = "键盘映射" if cal.get("input_mode") == "keyboard" else "屏幕坐标"
                done.append("③ 四点校准")
                if hasattr(self, "_lbl_cal"):
                    self._lbl_cal.configure(
                        text=f"✓ 已从预设导入校准（{n} 键，{mode}）", foreground="#1a5fb4")
            except Exception as exc:  # noqa: BLE001
                self.log(f"⚠ 校准写入失败：{exc}")

        # 7) ④ 逐键音色样本库（写回文件；延迟仅参考）
        blob = lat.get("payload")
        prof = preset.unpack_blob(blob) if blob else None
        if isinstance(prof, dict) and prof:
            try:
                Path(C.latency_profile_path).write_text(
                    json.dumps(prof, indent=2, ensure_ascii=False), encoding="utf-8")
                done.append("④ 延迟记录（仅参考）")
                if hasattr(self, "_lbl_latency"):
                    self._lbl_latency.configure(text="✓ 已从预设导入延迟记录（仅参考）",
                                                foreground="#1a5fb4")
            except Exception as exc:  # noqa: BLE001
                self.log(f"⚠ 延迟记录写入失败：{exc}")

        # 8) 焦点守卫的目标窗口
        blob = state.get("focus_target", {}).get("payload")
        ft = preset.unpack_blob(blob) if blob else None
        if isinstance(ft, dict) and ft:
            try:
                from piano_tool.focus_guard import TARGET_PATH, load_target

                Path(TARGET_PATH).write_text(
                    json.dumps(ft, indent=2, ensure_ascii=False), encoding="utf-8")
                self._focus_target = load_target()
                done.append("⑤ 焦点目标窗口")
                try:
                    self._update_focus_hint()
                except Exception:  # noqa: BLE001
                    pass
            except Exception as exc:  # noqa: BLE001
                self.log(f"⚠ 焦点目标写入失败：{exc}")

        # 9) 全量预设里的歌曲 MIDI
        song = state.get("song", {})
        if song.get("midi_b64"):
            data = preset.bytes_of(song.get("midi_b64"))
            if data:
                name_ = Path(str(song.get("midi_name") or "song.mid")).name
                dst = Path(C.output_dir) / name_
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(data)
                    self._var_midi.set(str(dst))
                    done.append(f"歌曲 MIDI（{name_}）")
                except Exception as exc:  # noqa: BLE001
                    self.log(f"⚠ 歌曲 MIDI 还原失败：{exc}")

        # 10) 每键采样（编辑器「模拟音」的音源）——导入了就不用再录一遍
        sec = state.get("samples", {})
        if sec:
            try:
                got, bad = sample_bank.restore(sec, Path(C.data_dir) / "key_samples")
                if got:
                    done.append(f"每键采样 {got} 键（模拟音）")
                    self.log(f"✓ 已恢复每键采样：{got} 个键，编辑器「模拟音」可直接用"
                             + (f"（{len(bad)} 个键的数据坏了，已跳过：{', '.join(bad[:6])}）"
                                if bad else ""))
                elif bad:
                    self.log(f"⚠ 预设里有采样段，但 {len(bad)} 个键的数据都读不出来："
                             f"{', '.join(bad[:6])}")
            except Exception as exc:  # noqa: BLE001
                logger.exception("restore samples failed")
                self.log(f"⚠ 每键采样还原失败：{exc}")
        return done

    def _on_import_preset(self):
        p = filedialog.askopenfilename(
            title="导入用户预设",
            filetypes=[("预设文件", "*.ini"), ("所有文件", "*.*")])
        if not p:
            return
        try:
            state = preset.read_preset(p)
        except preset.PresetError as exc:
            messagebox.showerror("导入失败", str(exc))
            return
        try:
            done = self._apply_preset_state(state)
        except Exception as exc:  # noqa: BLE001
            logger.exception("import preset failed")
            messagebox.showerror("导入失败", f"{type(exc).__name__}: {exc}")
            return
        detail = "、".join(done) if done else "内容与当前设置相同"
        self.log(f"✓ 已导入预设：{Path(p).name}（{detail}）")
        messagebox.showinfo("导入完成", f"已应用预设：\n{detail}")

    def _on_export_preset(self, with_song: bool):
        default_name = "用户预设_含歌曲.ini" if with_song else preset.DEFAULT_NAME
        title = "导出用户预设（含歌曲 MIDI）" if with_song else "导出用户预设（不含 MIDI）"
        p = filedialog.asksaveasfilename(
            title=title, defaultextension=".ini", initialfile=default_name,
            filetypes=[("预设文件", "*.ini"), ("所有文件", "*.*")])
        if not p:
            return
        state = self._preset_state(with_song)
        try:
            preset.write_preset(p, state)
        except Exception as exc:  # noqa: BLE001
            logger.exception("export preset failed")
            messagebox.showerror("导出失败", f"{type(exc).__name__}: {exc}")
            return
        has_song = "song" in state
        sec = state.get("samples") or {}
        n_smp = int(sec.get("count") or 0) if sec else 0
        mb = (int(sec.get("bytes") or 0) / 1048576.0) if sec else 0.0
        try:
            size_mb = Path(p).stat().st_size / 1048576.0
        except OSError:
            size_mb = 0.0

        parts = ["②③④⑤ 设置"]
        if has_song:
            parts.append(f"歌曲 MIDI（{state['song'].get('midi_name')}）")
        if n_smp:
            parts.append(f"每键采样 {n_smp} 个（{mb:.1f} MB）")
        detail = " + ".join(parts)

        self.log(f"✓ 已导出预设：{p}（{size_mb:.2f} MB；含 {detail}）"
                 + ("" if n_smp or not with_song else f"；采样：{getattr(self, '_sample_note', '')}"))
        extra = ""
        if with_song and not n_smp:
            note = getattr(self, "_sample_note", "")
            extra = f"\n\n注意：这次没带每键采样。{note}" if note else ""
        elif n_smp:
            extra = ("\n\n导入这份预设时会一并恢复这些采样，"
                     "编辑器「模拟音」不用再录一遍。")
        messagebox.showinfo(
            "导出完成",
            f"已保存到：\n{p}\n\n文件大小：{size_mb:.2f} MB\n含：{detail}{extra}")

    def _save_session_preset(self):
        """关闭时把当前设置记下来，下次启动直接沿用（不用再手动调一遍）。"""
        try:
            preset.write_preset(Path(C.data_dir) / "last_session.ini",
                                self._preset_state(False))
        except Exception:  # noqa: BLE001
            logger.warning("保存上次设置失败", exc_info=True)

    def _load_session_preset(self):
        p = Path(C.data_dir) / "last_session.ini"
        if not p.exists():
            return
        try:
            done = self._apply_preset_state(preset.read_preset(p))
            if done:
                self.log(f"✓ 已沿用上次的设置：{'、'.join(done)}")
        except Exception:  # noqa: BLE001
            logger.warning("沿用上次设置失败", exc_info=True)

    def _frame_convert(self):
        f = ttk.Frame(self._nb, padding=16)
        self._var_audio = tk.StringVar()
        if self._bp_ok:
            tip = ("上传/选择歌曲音频（MP3/WAV），用 basic-pitch 复音转写为 MIDI。\n"
                   "提示：整首混音直接转写，细节损失最大。效果最好的做法是找现成的原曲 MIDI，"
                   "拿到「② MIDI→琴谱」直接生成；音频转写是兜底。转写完记得在②页勾"
                   "「分层独立八度」、关掉「只保留主旋律」（这两项已是推荐默认）。")
            state = "normal"
        else:
            tip = "✗ 未检测到 basic_pitch —— 请用 启动.bat 启动，或检查 .venv 依赖。"
            state = "disabled"
        ttk.Label(f, text=tip, wraplength=900).grid(row=0, column=0, columnspan=3, sticky="w")
        if not self._bp_ok:
            ttk.Label(f, text="当前 Python 缺少 basic_pitch/tensorflow 依赖（双击 desktop_app.py 已会自动尝试切到 .venv）。",
                      foreground="#c0392b").grid(row=1, column=0, columnspan=3, sticky="w")

        ttk.Label(f, text="音频文件：").grid(row=2, column=0, sticky="e", pady=(14, 4))
        ttk.Entry(f, textvariable=self._var_audio, width=80).grid(row=2, column=1, sticky="we", pady=(14, 4))
        ttk.Button(f, text="浏览…", command=self._browse_audio).grid(row=2, column=2, padx=4, pady=(14, 4))

        self._btn_convert = ttk.Button(f, text="开始转写", command=self._on_convert, state=state)
        self._btn_convert.grid(row=3, column=1, sticky="w", pady=10)

        # 「先人声分离再转写」：整首混音里伴奏/鼓点会被一起识进 MIDI，
        # 先用 Demucs 把人声（≈主旋律）剥出来，转写质量明显提升。
        self._demucs_ok = False
        try:
            from piano_tool.vocal_separation import is_demucs_available
            self._demucs_ok = is_demucs_available()
        except Exception:  # noqa: BLE001
            self._demucs_ok = False
        # 未安装时不置灰：勾选时才提示下载（不在启动时偷偷装几百 MB）
        self._demucs_installing = False
        self._demucs_t0 = 0.0
        self._var_vocalsep = tk.BooleanVar(value=bool(self._demucs_ok))
        self._cb_vocalsep = ttk.Checkbutton(
            f, text=self._vocalsep_text(), variable=self._var_vocalsep,
            command=self._on_vocalsep_toggle,
        )
        self._cb_vocalsep.grid(row=3, column=2, sticky="w", padx=8)

        # ---- 人声分离固定用 CPU：分离本身开销不大（几十秒~几分钟），
        # 不再提供 GPU 模式、也不再拉 2.3GB 的 CUDA 版 PyTorch——
        # 装/卸载大包既费时又容易被安全软件误伤。
        self._var_sep_device = tk.StringVar(value="cpu")
        ttk.Label(f, text="分离计算设备：CPU（人声分离为轻量计算，无需额外显卡配置）",
                  foreground="#666").grid(row=4, column=0, columnspan=3,
                                          sticky="w", pady=(2, 0))

        ttk.Label(f, text="转写完成后自动生成 data/output/song.mid，请到「② MIDI→琴谱」继续。",
                  foreground="#666").grid(row=5, column=0, columnspan=3, sticky="w")

        f.columnconfigure(1, weight=1)
        return f

    def _frame_map(self):
        f = ttk.Frame(self._nb, padding=16)
        self._var_midi = tk.StringVar(value=str(C.output_dir / "song.mid"))

        # ---- 乐器选择：整页的音域/键位表都跟着它走 ----
        inst_row = ttk.Frame(f)
        inst_row.grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(inst_row, text="乐器：").pack(side="left")
        self._var_instrument = tk.StringVar(value=self._instrument_name())
        self._combo_instrument = ttk.Combobox(
            inst_row, textvariable=self._var_instrument, width=14, state="readonly",
            values=[i.name for i in instruments.all_instruments()],
        )
        self._combo_instrument.pack(side="left", padx=(2, 8))
        self._combo_instrument.bind("<<ComboboxSelected>>",
                                    lambda _e: self._on_instrument_change())
        self._lbl_instrument = ttk.Label(inst_row, text="", foreground="#1a5fb4")
        self._lbl_instrument.pack(side="left")

        self._var_autotp = tk.BooleanVar(value=True)
        # 场景默认（按「改编一首完整的歌」设的）：主旋律+和声都要，默认不再砍伴奏；
        # 低/中/高分层默认开，低音不再被折进旋律区搅浑。
        self._var_mono = tk.BooleanVar(value=False)
        self._var_melody_mode = tk.StringVar(value="high")
        self._var_oor = tk.StringVar(value="drop")   # drop=丢弃超音域, fold=折叠八度
        self._lbl_map_intro = ttk.Label(
            f, text="", wraplength=900, justify="left")
        self._lbl_map_intro.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        ttk.Label(f, text="MIDI 文件：").grid(row=2, column=0, sticky="e", pady=(14, 4))
        self._ent_midi = ttk.Entry(f, textvariable=self._var_midi, width=80)
        self._ent_midi.grid(row=2, column=1, sticky="we", pady=(14, 4))
        ttk.Button(f, text="浏览…", command=self._browse_midi).grid(row=2, column=2, padx=4, pady=(14, 4))

        opt = ttk.Frame(f)
        opt.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # 「分层 / 声部整理方式」——一个模块管全部：6 个单选 + 1 个降糊下拉。
        # 前三项是新算法（忠实全曲 + 按声部分八度）：
        #   track=独立分轨（按音轨/单轨时按音高，推荐）/ pitch=按音高分三段 /
        #   piano=钢琴曲适配（按旋律/低音/和声角色改编，钢琴谱推荐）；
        # 后三项是老行为（arrange=none，走旧代码路径，不降同时音数）：
        #   old_pitch=A 按音高分层 / old_track=B 按音轨分层 / off=不分层。
        # 默认「老行为·A 按音高分层」，和以前完全一样。
        self._var_arrange = tk.StringVar(value="old_pitch")
        # 同时最多几个音（降糊用）。默认 4 = 老版本；选中「钢琴曲适配」时
        # _sync_arrange_ui 会自动拉回 3（原琴谱最多 3 个键）。
        self._var_polyphony = tk.StringVar(value="4")
        # 黑键走向改编：上行取上邻键、下行取下邻键（默认开，比无脑就近吸附更像原曲）
        self._var_contour = tk.BooleanVar(value=True)
        lf = ttk.LabelFrame(
            f, text=" 分层 / 声部整理方式（按声部分到不同八度，细节分明不糊） ",
            padding=8)
        lf.grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        # 这行说明写死了各层对应的琴排，换乐器就不对了，所以做成会在换乐器时
        # 刷新的标签（见 _refresh_instrument_texts → _update_arrange_hint）。
        self._lbl_layer_legend = ttk.Label(lf, text="", foreground="#666",
                                           wraplength=880, justify="left")
        self._lbl_layer_legend.pack(anchor="w", pady=(2, 6))
        arr_row1 = ttk.Frame(lf)
        arr_row1.pack(anchor="w", fill="x")
        ttk.Label(lf, text="以下为老行为（旧算法，不降同时音数）：",
                  foreground="#666").pack(anchor="w", pady=(6, 0))
        arr_row2 = ttk.Frame(lf)
        arr_row2.pack(anchor="w", fill="x", pady=(2, 0))
        self._arrange_rbs = [
            ttk.Radiobutton(
                arr_row1, text="独立分轨（各音轨/声部分占一个八度，推荐）",
                value="track", variable=self._var_arrange,
                command=self._on_arrange_changed),
            ttk.Radiobutton(
                arr_row1, text="按音高分三段（低/中/高）",
                value="pitch", variable=self._var_arrange,
                command=self._on_arrange_changed),
            ttk.Radiobutton(
                arr_row1, text="钢琴曲适配（旋律/低音/和声，钢琴谱推荐）",
                value="piano", variable=self._var_arrange,
                command=self._on_arrange_changed),
            ttk.Radiobutton(
                arr_row2, text="A 按音高分层（低/中/高各一段）",
                value="old_pitch", variable=self._var_arrange,
                command=self._on_arrange_changed),
            ttk.Radiobutton(
                arr_row2, text="B 按 MIDI 音轨分层（每轨一个八度）",
                value="old_track", variable=self._var_arrange,
                command=self._on_arrange_changed),
            ttk.Radiobutton(
                arr_row2, text="不分层（所有音一起折叠）",
                value="off", variable=self._var_arrange,
                command=self._on_arrange_changed),
        ]
        for i, w in enumerate(self._arrange_rbs):
            w.pack(side="left", padx=(0, 18) if i % 3 != 2 else (0, 0))
        self._lbl_arrange = ttk.Label(
            lf, text="", foreground="#1a5fb4", wraplength=880, justify="left")
        self._lbl_arrange.pack(anchor="w", pady=(4, 0))
        # 同时最多几个音：转写常把一首歌唱成十几个音一起响，糊成一片。这里限制
        # 同时发声数（永远保留最高的旋律和最低的贝斯），让音符分明、跟得上节奏；
        # 只减音数、不改起音/时值，所以节奏和原曲完全一致。
        poly_row = ttk.Frame(lf)
        poly_row.pack(anchor="w", fill="x", pady=(4, 0))
        ttk.Label(poly_row, text="同时最多几个音：").pack(side="left")
        self._cb_polyphony = ttk.Combobox(
            poly_row, textvariable=self._var_polyphony, width=4, state="readonly",
            values=["2", "3", "4", "5", "6"])
        self._cb_polyphony.pack(side="left")
        ttk.Label(poly_row, text="（默认 4 = 老版本；原琴谱建议 2~3；"
                                 "钢琴适配会自动按 3 生成）").pack(side="left", padx=(6, 0))
        self._on_arrange_changed()  # 按默认「老行为·A」初始化状态 + 文案

        ttk.Checkbutton(opt, text="自动移调到白键友好调性（强烈建议开启）",
                        variable=self._var_autotp).pack(side="left")
        ttk.Checkbutton(opt, text="只保留主旋律（单音）", variable=self._var_mono).pack(side="left", padx=16)
        ttk.Label(opt, text="取音：").pack(side="left")
        ttk.Combobox(opt, textvariable=self._var_melody_mode, width=6, state="readonly",
                     values=["high", "loud", "vocal"]).pack(side="left")
        ttk.Label(opt, text="(high=最高音, loud=最响, vocal=连续人声线/人声推荐)").pack(side="left", padx=(4, 12))
        ttk.Label(opt, text="超音域音：").pack(side="left")
        self._cb_oor = ttk.Combobox(opt, textvariable=self._var_oor, width=6, state="readonly",
                                    values=["drop", "fold"])
        self._cb_oor.pack(side="left")
        ttk.Label(opt, text="(drop=丢弃低音贝斯, fold=折叠八度)").pack(side="left", padx=4)
        ttk.Checkbutton(opt, text="黑键走向改编（推荐）",
                        variable=self._var_contour).pack(side="left", padx=(16, 0))

        row = ttk.Frame(f)
        row.grid(row=5, column=0, columnspan=3, sticky="w", pady=10)
        self._btn_map = ttk.Button(row, text="生成琴谱", command=self._on_map)
        self._btn_map.pack(side="left")
        self._btn_editor = ttk.Button(row, text="在浏览器中微调（编辑器）",
                                      command=self._on_open_editor)
        self._btn_editor.pack(side="left", padx=8)
        self._btn_notation = ttk.Button(row, text="导出按键谱(txt)",
                                        command=self._on_export_notation)
        self._btn_notation.pack(side="left")

        # 「人声匹配」：人声一次只唱一个字 —— 把歌词的一字一音配到已有旋律上。
        # 只改「每个音唱哪个字」和时值归属，不改调。
        self._btn_vocal = ttk.Button(row, text="🎤 人声匹配（一字一音）",
                                     command=self._on_vocal_match)
        self._btn_vocal.pack(side="left", padx=(8, 0))

        # 「清理音符保持旋律」：把背景音/噪声误识成的极短米粒音清掉
        self._var_mindur = tk.DoubleVar(value=0.06)
        ttk.Label(row, text="米粒阈值").pack(side="left", padx=(12, 2))
        ttk.Spinbox(row, from_=0.02, to=0.30, increment=0.01, width=6,
                    textvariable=self._var_mindur).pack(side="left")
        ttk.Label(row, text="秒(短于此值视为背景噪声)").pack(side="left", padx=(2, 8))
        self._btn_clean = ttk.Button(row, text="清理音符保持旋律",
                                     command=self._on_clean_events)
        self._btn_clean.pack(side="left")

        # 「高音柔化」：过高/跳进过硬的音做八度平移（音级与调性不变，只换八度）
        row2 = ttk.Frame(f)
        row2.grid(row=6, column=0, columnspan=3, sticky="w", pady=(0, 2))
        ttk.Label(row2, text="高音柔化：").pack(side="left")
        self._var_ceiling = tk.StringVar(value="")
        # 可选的「上限音」随乐器音域生成（风物之诗琴有 B6，首席女高音最高只到 B5）
        self._cb_ceiling = ttk.Combobox(row2, textvariable=self._var_ceiling, width=8,
                                        state="readonly", values=[])
        self._cb_ceiling.pack(side="left")
        cb_ceiling = self._cb_ceiling
        ttk.Label(row2, text="以上的音降八度").pack(side="left", padx=(2, 10))
        ttk.Label(row2, text="最大跳进").pack(side="left")
        self._var_maxleap = tk.StringVar(value="12")
        cb_leap = ttk.Combobox(row2, textvariable=self._var_maxleap, width=5,
                               state="readonly", values=["不限", "24", "12", "9", "7"])
        cb_leap.pack(side="left", padx=(2, 2))
        ttk.Label(row2, text="半音(超出则就近换八度)").pack(side="left", padx=(0, 8))
        self._btn_soften = ttk.Button(row2, text="高音柔化", command=self._on_soften)
        self._btn_soften.pack(side="left")
        cb_ceiling.bind("<<ComboboxSelected>>", lambda _e: self._update_soften_hint())
        cb_leap.bind("<<ComboboxSelected>>", lambda _e: self._update_soften_hint())

        self._lbl_soften_hint = ttk.Label(f, foreground="#666", wraplength=900,
                                          justify="left")
        self._lbl_soften_hint.grid(row=7, column=0, columnspan=3, sticky="w", pady=(0, 6))

        # 「音符整理」：起点/时值对齐拍网格（只动节奏与长短，不改音高旋律）
        row3 = ttk.Frame(f)
        row3.grid(row=8, column=0, columnspan=3, sticky="w", pady=(0, 2))
        ttk.Label(row3, text="音符整理：").pack(side="left")
        self._var_grid = tk.StringVar(value="1/8(八分)")
        ttk.Combobox(row3, textvariable=self._var_grid, width=11, state="readonly",
                     values=["1/4(四分)", "1/8(八分)", "1/16(十六分)"]).pack(side="left")
        ttk.Label(row3, text="网格").pack(side="left", padx=(2, 10))
        ttk.Label(row3, text="BPM").pack(side="left")
        self._var_bpm = tk.DoubleVar(value=0)
        ttk.Spinbox(row3, from_=0, to=240, increment=1, width=6,
                    textvariable=self._var_bpm).pack(side="left", padx=(4, 2))
        ttk.Label(row3, text="(0=自动估计)").pack(side="left", padx=(0, 10))
        self._btn_organize = ttk.Button(row3, text="音符整理",
                                        command=self._on_organize)
        self._btn_organize.pack(side="left")

        # 注意：这一行必须独占 row=8。它以前和 row3 同为 row=7，
        # 两个组件抢同一个 grid 单元格，后画的 row3 会把这行提示盖住。
        ttk.Label(f,
                  text="提示：把音符的起点和长短对齐到拍子网格，节奏更稳、时值统一；"
                       "同一个音被切碎时会自动合并成一个长音。"
                       "只动节奏和长短，音高旋律完全不改。\n"
                       "网格/BPM 是对齐用的标尺：1/4 网格更粗（更多音被吸附、合并），1/16 更细（基本不动）；"
                       "BPM(0=自动估计) 只决定格子的疏密，**不是倍速** —— 想变速去 ⑤ 页调「倍速」。"
                       "整理是幂等的：琴谱已经在网格上时再点不会变。",
                  foreground="#666", wraplength=900, justify="left").grid(
            row=9, column=0, columnspan=3, sticky="w", pady=(0, 6))

        self._lbl_map_stats = ttk.Label(f, text="（尚未生成琴谱）", foreground="#666",
                                        wraplength=900, justify="left")
        self._lbl_map_stats.grid(row=10, column=0, columnspan=3, sticky="w")
        f.columnconfigure(1, weight=1)
        # 先按默认乐器把上面这些随乐器变化的文案填一遍
        self._refresh_instrument_texts()
        return f

    # ---- ② 「分层 / 声部整理方式」：一个模块 6 选 1 + 降糊下拉 ----
    # 单选值 → map_midi_to_events 的 (arrange, layered_octave, layer_mode)：
    #   track     新算法·独立分轨（按音轨/单轨时按音高，推荐）
    #   pitch     新算法·按音高分三段
    #   piano     新算法·钢琴曲适配（旋律/低音/和声角色改编）
    #   old_pitch 老行为·A 按音高分层
    #   old_track 老行为·B 按 MIDI 音轨分层
    #   off       老行为·不分层（所有音一起折叠）
    ARRANGE_CHOICES = ("track", "pitch", "piano", "old_pitch", "old_track", "off")

    def _arrange_params(self) -> Tuple[str, bool, str]:
        """当前 6 选 1 → (arrange, layered_octave, layer_mode)。"""
        m = self._var_arrange.get()
        if m == "track":
            return "track", True, "track"
        if m == "pitch":
            return "pitch", True, "pitch"
        if m in ("piano", "raw"):
            # raw = 旧预设里的「原样多声部」。用户明确要求把它换成
            # 「钢琴曲适配」，所以旧值也走新算法，不再保留原样堆叠。
            return "piano", False, "pitch"
        if m == "old_track":
            return "none", True, "track"
        if m == "old_pitch":
            return "none", True, "pitch"
        return "none", False, "pitch"   # off = 老行为·不分层

    def _on_arrange_changed(self):
        """6 选 1 变了：刷新可用状态 + 白话说明。"""
        self._sync_arrange_ui()
        self._update_arrange_hint()

    def _sync_arrange_ui(self):
        """人声乐器禁用新整理方式（旋律由唱法层落位）；
        降糊下拉只对「独立分轨 / 按音高 / 钢琴曲适配」有意义。"""
        mono = instruments.active().monophonic
        mode = self._var_arrange.get() if hasattr(self, "_var_arrange") else "old_pitch"
        # 旧预设/旧存档里的 raw → 统一显示成「钢琴曲适配」
        if mode == "raw":
            self._var_arrange.set("piano")
            mode = "piano"
        if mono and mode in ("track", "pitch", "piano", "raw"):
            # 人声乐器：整理方式不生效，切回老行为·不分层
            self._var_arrange.set("off")
            mode = "off"
        rbs = getattr(self, "_arrange_rbs", [])
        for w, val in zip(rbs, self.ARRANGE_CHOICES):
            try:
                w.configure(state="disabled" if (
                    mono and val in ("track", "pitch", "piano")) else "normal")
            except Exception:  # noqa: BLE001
                pass
        try:
            self._cb_polyphony.configure(
                state="readonly" if (
                    not mono and mode in ("track", "pitch", "piano")) else "disabled")
        except Exception:  # noqa: BLE001
            pass
        # 钢琴适配按原琴谱习惯最多 3 个键；用户之前存过 4~6 的话这里拉回 3
        if mode == "piano":
            try:
                if int(self._var_polyphony.get()) > 3:
                    self._var_polyphony.set("3")
            except (TypeError, ValueError):
                pass

    def _update_arrange_hint(self):
        """刷新模块下方的白话说明 + 各层琴排图例（按当前乐器生成）。"""
        if not hasattr(self, "_lbl_arrange"):
            return
        inst = instruments.active()
        n = len(inst.keys)
        bands = inst.octave_bands()
        mode = self._var_arrange.get()
        if mode == "piano":
            self._lbl_layer_legend.configure(
                text="钢琴曲适配按声部角色分排：旋律→中/高排，低音→底排，"
                     "和声→中间排；不再按音高硬切三段。")
        else:
            # 每一层一段，带上对应那一排的键：风物之诗琴 3 段、首席女高音 2 段
            seg = "，".join(
                "第%d层往 %d~%d（%s %s~%s）靠" % (
                    i + 1, b[0], b[1], inst.rows[i].label,
                    inst.rows[i].letters[0], inst.rows[i].letters[-1],
                )
                for i, b in enumerate(bands)
            )
            self._lbl_layer_legend.configure(
                text=f"分层按琴排切：{seg}，每层互不重叠，低音就不会被折到旋律区里"
                     f"把声音搅浑；选「不分层」就是以前的老做法（所有音一起折叠进 {n} 键）。")
        if inst.monophonic:
            self._lbl_arrange.configure(
                text="（人声乐器：旋律整条落位由唱法层处理，三种整理方式不生效，"
                     "用下面的老行为即可。）")
            return
        if mode == "track":
            self._lbl_arrange.configure(
                text="推荐·独立分轨：忠实保留全曲每个音、节奏和原曲一致；按 MIDI 音轨"
                     "（只有一个音轨时整体落位）把各声部分到不同八度。另按原琴谱习惯"
                     "把旋律整体抬到中/高排、底排留给低音 —— 旋律会更亮，不会又低又闷。")
        elif mode == "pitch":
            self._lbl_arrange.configure(
                text="按音高把低/中/高三段分别落到键盘三行，各段一个八度、互不重叠，"
                     "低音不会折进旋律区把声音搅浑。")
        elif mode == "piano":
            self._lbl_arrange.configure(
                text="钢琴曲适配：按时长加权识别调性并整体移到自然音阶（调内音全变白键）；"
                     "再按「旋律 / 低音 / 和声」分排：旋律只留最高 1 个到中/高排、低音到底排、和声到中排；转写造成的孤立八度尖峰会折回。"
                     "每簇和弦音优先保留、去八度重复、去半音冲突，最多 3 个键；长音不再占复音位（原琴是拨弦）。"
                     "「同时最多几个音」就是和弦厚度：原琴谱最常用 **3**"
                     "（旋律1 + 低音1 + 和声1），2 = 只保旋律+低音；"
                     "4 以上按不过来、也容易糊，钢琴适配会自动按 3 生成。"
                     "键盘弹奏保留同时和弦；鼠标弹奏会自动拆成短促琶音。"
                     "「取音」和「超音域」两项对钢琴适配不生效（旋律固定用天际线，"
                     "音域内的音按角色归位、音域外的音按②页设置丢弃/折叠。")
        elif mode == "old_pitch":
            self._lbl_arrange.configure(
                text="老行为·A 按音高分层：低/中/高三段各往键盘对应那一排靠"
                     "（各段一个八度、互不重叠），不降同时音数。")
        elif mode == "old_track":
            self._lbl_arrange.configure(
                text="老行为·B 按 MIDI 音轨分层：每个音轨各挑一个八度偏移"
                     "（多轨时各轨错开）；只有一个音轨时，效果和 A 一样。")
        else:
            self._lbl_arrange.configure(
                text="老行为·不分层：所有音一起折叠进琴键 —— 和以前完全一样。")

    def _update_soften_hint(self):
        """刷新柔化参数下方的白话提示（文案逻辑见 soften_hint_text）。"""
        self._lbl_soften_hint.configure(
            text=soften_hint_text(self._var_ceiling.get(), self._var_maxleap.get())
        )

    # ---- ② 选乐器：整页的音域/键位表都跟着换 ----
    def _instrument_name(self) -> str:
        """界面上当前选中的乐器名（以 instruments 里那份为准）。"""
        return instruments.active().name

    def _on_instrument_change(self):
        """切换乐器：让 instruments.active() 跟着换，并把 ② 页参数切到该乐器的预设。"""
        inst = instruments.use(self._var_instrument.get())
        self._apply_instrument_preset(inst)
        self._refresh_instrument_texts(inst)
        self.log(f"✓ 已切换乐器：{inst.name} —— {inst.range_desc()}")
        self.log(f"   {inst.rows_desc()}（低音在左、高音在右）")
        self.log(f"   音高依据：{inst.source}")
        if inst.monophonic:
            self.log("   人声乐器，映射参数已自动切换：")
            self.log("   · 只保留主旋律（单音）——人声一次只能唱一个音，和弦塞进来会糊成一团")
            self.log("   · 关闭分层独立八度 —— 那套是给多声部拆低/中/高音的，用在单声部旋律上"
                     "会把旋律中途拆成两段跳八度，最刺耳")
            self.log(f"   · 最大跳进收紧到 "
                     f"{instruments.active().preset()['max_leap']} 半音，上限音降到 "
                     f"{self._var_ceiling.get()}（歌手舒服音区）")
            desc = inst.articulation_desc()
            if desc:
                self.log("   · " + desc)
            self.log("   · 参考谱校准（四份共 451 音）：音域 C4~A5、中位 A4、95% 在 F5 以内、"
                     "全单音 → 中位音高跑出 C4~E5 时按 ±12 挪回来（只换八度、不改调）")
        else:
            self.log("   非人声乐器，映射参数已切回多声部默认（分层独立八度开、超音域折叠）。")
        self.log("⚠ 同一个字母键在两件乐器上发的音不一样，换乐器后请重新点「生成琴谱」，"
                 "旧的 events.json 是上一件乐器的键位。")

    def _apply_instrument_preset(self, inst):
        """把 ② 页的参数切到该乐器的预设（只切这一次，之后用户可以自己改）。"""
        p = inst.preset()
        self._var_mono.set(bool(p["mono"]))
        if inst.monophonic:
            # 人声取旋律用「连续人声线」：跟上一个音接轨，不被伴奏/和声的
            # 尖音带跑（skyline 在转写噪声多的时候会跳来跳去）
            self._var_melody_mode.set(str(p["melody_mode"]))
        elif self._var_melody_mode.get() == "vocal":
            # 「连续人声线」只对人声有意义，切回拨弦乐器时还原成 skyline
            self._var_melody_mode.set("high")
        if p["max_leap"]:
            self._var_maxleap.set(str(int(p["max_leap"])))
        # 上限音回到该乐器的默认（人声 = 舒服音区的顶 = G5）
        self._var_ceiling.set(self._default_ceiling(inst))
        # 人声只走「保轮廓整条落位」那一套：超音域按八度挪进音域、挪不进的才丢，
        # 所以「超音域音 drop/fold」这个下拉对人声没有意义，直接禁用免得误以为生效。
        if hasattr(self, "_cb_oor"):
            self._cb_oor.configure(state="disabled" if inst.monophonic else "readonly")
        # 「分层 / 声部整理方式」：人声乐器三种整理方式不生效 → 老行为·不分层
        # （预设 layered=False）；拨弦乐器若停在「不分层」上，按预设落回
        # 老行为·A（用户自选的新整理方式 / 老行为 B 保留不动）。
        if hasattr(self, "_var_arrange"):
            if inst.monophonic:
                if not p["layered"]:
                    self._var_arrange.set("off")
                elif self._var_arrange.get() not in ("old_pitch", "old_track"):
                    self._var_arrange.set("old_pitch")
            elif self._var_arrange.get() == "off" and p["layered"]:
                self._var_arrange.set("old_pitch")
            self._sync_arrange_ui()
            self._update_arrange_hint()
        self._update_soften_hint()

    @staticmethod
    def _ceiling_options(inst) -> List[str]:
        """高音柔化可选的「上限音」：当前乐器音域里的几个高音（C / G / A / B）。"""
        vals = ["不限"]
        for m in sorted(inst.white_midis, reverse=True):
            if m % 12 in (0, 7, 9, 11):
                vals.append(f"{instruments.midi_name(m)}({m})")
            if len(vals) >= 7:
                break
        return vals

    @staticmethod
    def _default_ceiling(inst) -> str:
        """默认上限音（只影响「高音柔化」这一次动作）：
        · 乐器给了 soft_ceiling → 用它（首席女高音 = G5，参考谱最高只到 E5，留余量）
        · 否则有 C6(84) 就还用 C6(84)（风物之诗琴的老默认不变），再否则取音域最高音
        """
        m = inst.soft_ceiling
        if m is None and inst.comfort_band:
            m = int(inst.comfort_band[1])
        if m is None:
            m = 84 if 84 in inst.white_midis else inst.highest_midi
        return f"{instruments.midi_name(m)}({m})"

    def _refresh_instrument_texts(self, inst=None):
        """把界面上所有跟「音域 / 键位数 / 键位排布」有关的文案按当前乐器刷新。"""
        inst = inst or instruments.active()
        n = len(inst.keys)
        if hasattr(self, "_lbl_instrument"):
            self._lbl_instrument.configure(
                text=f"{inst.range_desc()} · {inst.hint}")
        if hasattr(self, "_lbl_map_intro"):
            extra = ""
            if inst.monophonic:
                extra = ("\n人声乐器：只取主旋律（一次只能唱一个音），超音域的音按八度挪进"
                         "音域（挪不进的才丢），**不做逐音各自折叠** —— 逐音折叠会在音域边界"
                         "把旋律劈出八度大跳，听起来刺耳。")
            self._lbl_map_intro.configure(
                text=f"把 MIDI 里的每个音符折叠/替换到「{inst.name}」的 {n} 个白键，"
                     f"生成 events.json 琴谱。\n"
                     f"本琴只有白键（{inst.rows_desc()}，低音在左、高音在右），"
                     f"默认会自动移调到最「白键友好」的调，避免旋律音高被改掉。{extra}")
        if hasattr(self, "_cb_ceiling"):
            values = self._ceiling_options(inst)
            self._cb_ceiling.configure(values=values)
            if self._var_ceiling.get() not in values:
                self._var_ceiling.set(self._default_ceiling(inst))
        # ③ 页那几处「21 键」的文字、④ 页的键数统计
        if hasattr(self, "_btn_compute"):
            self._btn_compute.configure(text=f"② 计算 {n} 键并保存")
        if hasattr(self, "_lbl_keyopt_legend"):
            self._lbl_keyopt_legend.configure(
                text=f"：{inst.rows_desc()}（低音在左、高音在右），"
                     f"播放时直接敲键盘，不用截图也不用四点校准。"
                     f"不勾就是以前的老方案（截屏 + 鼠标点击），这里不会改动你的屏幕校准文件。")
        if hasattr(self, "_lbl_input_mode"):
            self._update_keyopt_hint()
        if hasattr(self, "_combo_testkey"):
            # ⑤ 页的「自检键」下拉也得跟着换：有些字母在新乐器上并不发声
            values = [str(k["key"]) for k in inst.keys]
            self._combo_testkey.configure(values=values)
            if self._var_testkey.get() not in values and values:
                self._var_testkey.set(values[0])
        self._update_arrange_hint()
        self._update_soften_hint()

    def _frame_calibrate(self):
        f = ttk.Frame(self._nb, padding=8)

        # ---------------- 弹奏方式：默认老方案，勾一下才开「原神键位优化」 ----------------
        mode_f = ttk.LabelFrame(f, text=" 弹奏方式（默认老方案，勾一下才切成键盘弹奏） ", padding=10)
        mode_f.pack(fill="x", pady=(0, 8))
        self._var_keyopt = tk.BooleanVar(value=False)

        key_row = ttk.Frame(mode_f)
        key_row.pack(fill="x")
        ttk.Checkbutton(
            key_row, text="原神键位优化（用键盘弹奏，不用截屏）",
            variable=self._var_keyopt, command=self._on_keyopt_change,
        ).pack(side="left", anchor="w")
        # 键位说明随 ② 页选的乐器变（两件乐器的键位排布不一样）
        self._lbl_keyopt_legend = ttk.Label(key_row, text="", foreground="#666")
        self._lbl_keyopt_legend.pack(side="left", anchor="w")

        self._lbl_input_mode = ttk.Label(mode_f, text="", wraplength=940,
                                         justify="left", foreground="#1a5fb4")
        self._lbl_input_mode.pack(anchor="w", pady=(6, 0))
        self._sync_keyopt_from_file()

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 6))

        # ---------------- 演奏显示器：锁定一块屏，只截这块、坐标也只按这块算 ----------------
        screen_row = ttk.Frame(f)
        screen_row.pack(fill="x", pady=(0, 6))
        ttk.Label(screen_row, text="演奏显示器：").pack(side="left")
        self._var_screen = tk.StringVar(value="")
        self._cb_screen = ttk.Combobox(screen_row, textvariable=self._var_screen,
                                       width=32, state="readonly")
        self._cb_screen.pack(side="left")
        self._cb_screen.bind("<<ComboboxSelected>>", self._on_screen_change)
        ttk.Button(screen_row, text="刷新", width=6,
                   command=self._refresh_screen_list).pack(side="left", padx=6)
        ttk.Label(screen_row,
                  text="（双屏请选模拟器所在那块；只存本机，不进预设文件）",
                  foreground="#666").pack(side="left", padx=6)
        self._screens: list = []
        self._shot_screen = None          # 当前截屏对应的那块屏
        self._refresh_screen_list(initial=True)

        ttk.Button(bar, text="① 截取屏幕", command=self._on_screenshot).pack(side="left")
        self._btn_compute = ttk.Button(bar, text="② 计算 21 键并保存",
                                       command=self._on_compute_calibration)
        self._btn_compute.pack(side="left", padx=8)
        ttk.Button(bar, text="查看调试图", command=self._on_open_debug).pack(side="left")
        self._lbl_cal = ttk.Label(bar, text="先截屏，再把 4 个红点拖到琴键区四角（顺序：左上→右上→右下→左下）",
                                  foreground="#666")
        self._lbl_cal.pack(side="left", padx=12)

        self._canvas = tk.Canvas(f, bg="#222", height=600, highlightthickness=0)
        self._canvas.pack(fill="both", expand=True)
        self._canvas.bind("<Button-1>", self._on_canvas_press)
        self._canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        # 填一次随乐器变化的文案（键位说明 / 键数按钮）
        self._refresh_instrument_texts()
        return f

    # ---- ③ 「原神键位优化」开关 ----
    def _update_keyopt_hint(self):
        inst = instruments.active()
        n = len(inst.keys)
        if bool(self._var_keyopt.get()):
            tip = (f"当前：原神键位优化已开启 —— 「{inst.name}」的 {n} 个白键直接就是 {n} 个字母键"
                   f"（{inst.rows_desc()}，低音在左、高音在右），播放时敲键盘（不是鼠标点）。"
                   "到 ⑤ 直接点「开始播放」即可；弹出来没声音就到 ⑤ 把「发送大写（Shift+字母）」勾上。"
                   "屏幕校准文件保持原样，取消勾选就回到鼠标点击的老方案。")
        else:
            tip = (f"当前：老方案（屏幕校准）—— 需要「① 截取屏幕」+ 拖 4 个角点 + "
                   f"计算 {n} 键并保存，播放时用鼠标点琴键坐标。"
                   "想改成直接敲键盘，就勾上面的「原神键位优化」。")
        self._lbl_input_mode.configure(text=tip)

    def _sync_keyopt_from_file(self):
        """历史兼容：老版本把键盘模式写进了 calibration.json（坐标全 0）。

        现在弹奏方式由勾选框决定、不再动文件，所以这里：
          · **绝不自动勾上「原神键位优化」** —— 不勾就必须是老方案；
          · 只提醒一句：磁盘上的校准是旧的键盘校准（坐标全 0），
            想用鼠标点击就重新「① 截取屏幕 → 计算 N 键并保存」覆盖它。
        """
        try:
            from piano_tool.player import load_calibration
            from piano_tool.keyboard_play import is_keyboard_calibration

            if is_keyboard_calibration(load_calibration(C.calibration_path)):
                n = len(instruments.active().keys)
                self.log("ℹ 提示：磁盘上的校准文件还是旧版「键盘校准」（坐标全 0，鼠标点不了）。"
                         f"想恢复鼠标点击的老方案，请在③ 重新「① 截取屏幕 → ② 计算 {n} 键并保存」。"
                         "想直接敲键盘，就勾选「原神键位优化」。")
        except Exception:  # noqa: BLE001  文件不存在/损坏就保持默认（不勾）
            pass
        self._update_keyopt_hint()

    def _on_keyopt_change(self):
        """勾选/取消「原神键位优化」：只影响播放方式，不碰 calibration.json。"""
        self._update_keyopt_hint()
        on = bool(self._var_keyopt.get())
        if on:
            msg = ("已开启「原神键位优化」：播放时直接敲键盘（Z~M / A~J / Q~U），"
                   "不用截图和四点校准。屏幕校准文件保持原样，取消勾选即回到老方案。")
        else:
            msg = "已回到老方案：播放时用鼠标点击琴键（需要③ 提前做好屏幕校准）。"
        self.log(("✓ " if on else "· ") + msg)

    # ---------------- 演奏显示器（③ 页） ----------------
    def _refresh_screen_list(self, initial: bool = False):
        """重新枚举显示器，按上次保存的选择或主屏选中。

        插拔显示器/改布局后点「刷新」重来 —— 保存的记录按「原点和尺寸」匹配，
        对不上就回落到主屏（不会拿着旧坐标乱点）。
        """
        from piano_tool import screens as screens_mod

        self._screens = screens_mod.list_screens()
        labels = [s.label for s in self._screens]
        self._cb_screen.configure(values=labels)
        if not self._screens:
            self._var_screen.set("（未检测到显示器信息，按主屏处理）")
            return
        saved = ui_scale.load_calib_screen()
        pick = screens_mod.find_screen(self._screens, saved) or screens_mod.pick_default(self._screens)
        self._var_screen.set(pick.label)
        if initial and len(self._screens) > 1:
            note = (f"检测到 {len(self._screens)} 块显示器，当前校准使用「{pick.label}」；"
                    "双屏请在这里选中模拟器所在的那块再截屏。")
            # 此时日志控件还没建好（本页在 Notebook 构造期间创建），排队等界面就绪
            self.after(200, lambda: self.log(note))

    def _selected_screen(self):
        """下拉里选中的那块屏（认不出就主屏）。"""
        from piano_tool import screens as screens_mod

        if not self._screens:
            self._screens = screens_mod.list_screens()
        label = str(self._var_screen.get() or "")
        for s in self._screens:
            if s.label == label:
                return s
        return screens_mod.pick_default(self._screens)

    def _on_screen_change(self, _evt=None):
        s = self._selected_screen()
        if s is None:
            return
        ui_scale.save_calib_screen(s.to_dict())      # 只存本机，不进预设
        # 旧截图属于上一块屏，清掉：不让人拿它算出「新屏原点 + 旧屏坐标」的错校准
        self._shot_bgr = None
        self._shot_screen = None
        self._corners = []
        self._cal = None
        if hasattr(self, "_canvas"):
            self._canvas.delete("all")
            self._photo = None
        self.log(f"已选「{s.label}」作为演奏屏（{s.width}×{s.height} @{s.left},{s.top}）。"
                 "换了屏要重新「① 截取屏幕」再校准，旧坐标是按上一块屏算的。")
        if len(self._screens) > 1:
            self._lbl_cal.configure(
                text=f"已切换到「{s.label}」——请点「① 截取屏幕」重新截屏并校准",
                foreground="#b58900")

    def _frame_latency(self):
        f = ttk.Frame(self._nb, padding=16)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(5, weight=1)

        ttk.Label(f, text="逐键弹一遍，采集每个键的真实琴声（data/key_samples/*.wav），\n"
                          "给浏览器编辑器的「🔈 模拟音」用。采样时顺带测到的延迟只作参考，\n"
                          "播放不再逐键补偿 —— 逐键提前量会打乱音与音的相对节奏。",
                  wraplength=900, justify="left").grid(row=0, column=0, columnspan=5, sticky="w")

        # 设备/控制区
        ctrl = ttk.Frame(f)
        ctrl.grid(row=1, column=0, columnspan=5, sticky="w", pady=(8, 4))
        ttk.Label(ctrl, text="录制设备：").pack(side="left")
        self._combo_audio_dev = ttk.Combobox(ctrl, width=46, state="readonly")
        self._combo_audio_dev.pack(side="left", padx=(4, 8))
        self._combo_audio_dev.bind("<<ComboboxSelected>>", lambda _e: self._on_device_selected())
        ttk.Button(ctrl, text="刷新设备", command=self._on_refresh_devices).pack(side="left")
        ttk.Button(ctrl, text="测试音频(2秒)", command=self._on_test_audio).pack(side="left", padx=(8, 0))
        # 多屏/多声卡时最常踩的坑：声音从扬声器出，却选了 HDMI 的回环 → 全静音
        ttk.Button(ctrl, text="🎯 找正在出声的", command=self._on_probe_devices).pack(side="left", padx=(8, 0))
        self._btn_sample = ttk.Button(ctrl, text="▶ 采集每键音色", command=self._on_sample_latency)
        self._btn_sample.pack(side="left", padx=(8, 0))
        self._lbl_device = ttk.Label(ctrl, text="", foreground="#1a5fb4")
        self._lbl_device.pack(side="left", padx=12)

        # 手动兜底
        manual = ttk.Frame(f)
        manual.grid(row=2, column=0, columnspan=5, sticky="w", pady=(4, 0))
        self._var_manual_latency = tk.IntVar(value=50)
        ttk.Label(manual, text="统一延迟记录：").pack(side="left")
        ttk.Spinbox(manual, from_=0, to=500, increment=5, width=6,
                    textvariable=self._var_manual_latency).pack(side="left")
        ttk.Label(manual, text="ms").pack(side="left", padx=(4, 8))
        ttk.Button(manual, text="保存记录（仅参考）", command=self._on_save_manual_latency).pack(side="left")
        ttk.Label(manual, text="（延迟不再参与播放；音频采不到时先检查设备/音量，不要靠猜延迟）",
                  foreground="#666").pack(side="left", padx=(8, 0))

        self._lbl_latency = ttk.Label(f, text="", foreground="#1a5fb4")
        self._lbl_latency.grid(row=3, column=0, columnspan=5, sticky="w", pady=(8, 0))

        ttk.Label(f, text="逐键采集结果（延迟仅参考；音色已存入 data/key_samples）：").grid(row=4, column=0, sticky="w", pady=(6, 2))

        # 文本 + 滚动条放进子框架，子框架用 grid 管理，内部也用 grid，避免与父框 grid 混用
        txt_frame = ttk.Frame(f)
        txt_frame.grid(row=5, column=0, columnspan=5, sticky="nsew", pady=(0, 0))
        txt_frame.rowconfigure(0, weight=1)
        txt_frame.columnconfigure(0, weight=1)
        self._lat_text = tk.Text(txt_frame, height=9, state="disabled", wrap="word")
        self._lat_text.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(txt_frame, command=self._lat_text.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self._lat_text.configure(yscrollcommand=sb.set)

        # 采样产物（每键 wav）的持久化说明：不少用户以为「关掉程序就没了」，
        # 其实存在 data/key_samples/；换机器/换新发布包时靠预设带走。
        ttk.Separator(f, orient="horizontal").grid(
            row=6, column=0, columnspan=5, sticky="we", pady=(10, 6))
        ttk.Label(
            f, text="每键录音存在 data/key_samples/（编辑器「模拟音」直接用它）。"
                    "换机器、换了新的发布包时，用顶部「导出全量(含MIDI/采样)…」把采样一起带走，"
                    "换过去导入预设就恢复，不用对着游戏再录一遍。",
            foreground="#666", wraplength=820, justify="left").grid(
                row=7, column=0, columnspan=4, sticky="w")
        ttk.Button(f, text="把采样存进预设…",
                   command=lambda: self._on_export_preset(True)).grid(
                       row=7, column=4, sticky="e")

        self._on_refresh_devices()
        return f

    def _clear_latency_text(self):
        self._lat_text.configure(state="normal")
        self._lat_text.delete("1.0", "end")
        self._lat_text.configure(state="disabled")

    def _on_refresh_devices(self):
        try:
            from piano_tool.latency_probe import list_capture_devices
            devs = list_capture_devices()
            self._audio_devices = devs
            if not devs:
                self._combo_audio_dev.configure(values=[], state="readonly")
                self._combo_audio_dev.set("")
                self._dev_label_to_index = {}
                self._dev_label_loopback = {}
                self._lbl_device.configure(
                    text="未枚举到任何音频设备，请检查声卡，或改用「手动统一延迟」",
                    foreground="#c0392b",
                )
                self.log("未枚举到任何音频设备。")
                return
            # 标签含设备索引，保证唯一；维护 标签 -> 设备索引 / 是否回环(系统声音)
            labels, self._dev_label_to_index, self._dev_label_loopback = [], {}, {}
            for d in devs:
                label = f"[{d['index']}] {d['name']}"
                labels.append(label)
                self._dev_label_to_index[label] = d["index"]
                self._dev_label_loopback[label] = bool(d.get("loopback"))
            self._combo_audio_dev.configure(values=labels, state="readonly")
            # 默认选中第一个「🎵 系统声音(回环)」——`list_capture_devices()` 已经把
            # 「系统默认输出那块」排在最前，所以这就是用户最可能要的那条。
            # 注意：回环项的 index 是 PyAudio 编号，不能拿 sounddevice 的默认输出索引去比。
            init_label = None
            for lab in labels:
                if self._dev_label_loopback.get(lab):
                    init_label = lab
                    break
            if init_label is None:
                init_label = labels[0]
            self._combo_audio_dev.set(init_label)
            n_loop = sum(1 for d in devs if d.get("loopback"))
            has_loopback_lib = True
            try:
                # 录系统声音靠 pyaudiowpatch 的 WASAPI loopback（sounddevice 做不到，
                # soundcard 在 Windows 上不稳且只认一块扬声器）
                import pyaudiowpatch  # noqa: F401
            except Exception:  # noqa: BLE001
                has_loopback_lib = False
            if n_loop and not has_loopback_lib:
                self._lbl_device.configure(
                    text=f"共 {len(devs)} 项，但录制系统声音需要 pyaudiowpatch（未安装）："
                         ".\\.venv\\Scripts\\python.exe -m pip install pyaudiowpatch。"
                         "装不上就改用 🎤「立体声混音」。",
                    foreground="#c0392b",
                )
            elif n_loop:
                self._lbl_device.configure(
                    text=f"共 {len(devs)} 项（{n_loop} 个「🎵 系统声音(回环)」）。"
                         "要采模拟器的声音请选 🎵 那一项——它不需要麦克风；"
                         "🎤 项会录入环境噪音。设备已按声卡去重。",
                    foreground="#1a5fb4",
                )
            else:
                self._lbl_device.configure(
                    text=f"共 {len(devs)} 项，但没有可用的「系统声音(回环)」——"
                         "系统里没有任何输出设备？可改用 🎤「立体声混音」，或改用手动统一延迟。",
                    foreground="#b58900",
                )
            loop_names = [d["name"] for d in devs if d.get("loopback")]
            self.log("音频捕获设备：" + "，".join(loop_names or [d["name"] for d in devs[:6]]))
        except Exception as exc:  # noqa: BLE001
            self._lbl_device.configure(text=f"无法枚举音频设备：{exc}", foreground="#c0392b")

    def _get_selected_device(self):
        """读取下拉框当前选中的设备索引；未选则返回 None（用默认输入设备）。"""
        combo = getattr(self, "_combo_audio_dev", None)
        label = combo.get().strip() if combo is not None else ""
        if not label:
            return None
        return self._dev_label_to_index.get(label)

    def _get_selected_loopback(self):
        """读取下拉框当前选中项是否为「系统声音(回环)」捕获（WASAPI loopback）。"""
        combo = getattr(self, "_combo_audio_dev", None)
        label = combo.get().strip() if combo is not None else ""
        if not label:
            return False
        return bool(self._dev_label_loopback.get(label, False))

    def _on_device_selected(self):
        name = self._combo_audio_dev.get()
        self._lbl_device.configure(text=f"已选择录制设备：{name}", foreground="#1a5fb4")
        self.log(f"已切换录制设备：{name}")

    def _on_probe_devices(self):
        """逐个回环设备听 0.45 秒，自动选中「正在出声的那块」。

        双屏/多声卡时最容易踩的坑：声音从笔记本喇叭出来，设备却选了 HDMI 显示器，
        于是 21 个键全采成静音、白等一分钟。这里让程序自己听。
        """
        if self._busy:
            return
        loops = [d for d in self._audio_devices if d.get("loopback")]
        if not loops:
            messagebox.showwarning("提示", "没有可用的「系统声音(回环)」设备，无法探测")
            return
        self._set_busy(True, self._btn_sample, "探测中…")
        self._lbl_latency.configure(
            text="正在逐个设备听电平（约 2 秒）…请让模拟器持续发声", foreground="#1a5fb4")
        threading.Thread(target=self._probe_devices_worker, args=(loops,), daemon=True).start()

    def _probe_devices_worker(self, loops):
        try:
            from piano_tool.latency_probe import probe_loopback_levels, quick_peak_level

            res = probe_loopback_levels(loops, seconds=0.45)
            best = max((float(r.get("peak") or 0) for r in res), default=0.0)
            inputs = [d for d in self._audio_devices if not d.get("loopback")]
            if best < 1e-3 and inputs:
                # 回环全线安静：顺手听听麦克风/立体声混音，别让用户干瞪眼
                self._q.put(("log", "回环设备都没声音，接着测输入设备（麦克风/立体声混音）…"))
                for d in inputs:
                    peak = quick_peak_level(device=d["index"], loopback=False, seconds=0.35)
                    res.append({"index": d["index"], "name": d["name"],
                                "peak": peak, "is_input": True})
            self._q.put(("probe_devices_done", res))
        except Exception as exc:  # noqa: BLE001
            self._q.put(("log", f"✗ 设备探测失败：{exc}"))
            self._q.put(("probe_devices_done", []))

    def _on_q_probe_devices_done(self, res):
        self._set_busy(False, self._btn_sample, "▶ 采集每键音色")
        if not res:
            self._lbl_latency.configure(text="探测失败：没有可用的采集设备", foreground="#c0392b")
            return
        ranked = sorted(res, key=lambda r: float(r.get("peak") or 0), reverse=True)
        detail = "，".join(
            f"{str(r.get('name', '')).split('·')[-1].strip()[:14]}={float(r.get('peak') or 0):.3f}"
            for r in ranked)
        self.log(f"设备电平（峰值）：{detail}")
        best = ranked[0]
        if float(best.get("peak") or 0) < 1e-3:
            self._lbl_latency.configure(
                text="所有设备都很安静 —— 确认模拟器正在发声、系统没静音、音量不为 0，然后再试一次",
                foreground="#c0392b")
            return
        for lab, idx in self._dev_label_to_index.items():
            if idx == best["index"] and bool(self._dev_label_loopback.get(lab)) == (not best.get("is_input")):
                self._combo_audio_dev.set(lab)
                break
        kind = "输入设备" if best.get("is_input") else "系统声音回环"
        self._lbl_latency.configure(
            text=f"✓ 已选最响的{kind}（电平 {float(best['peak']):.3f}）：{best['name']}，可以开始采样",
            foreground="#1a7a1a")

    def _on_test_audio(self):
        if self._busy:
            return
        self._clear_latency_text()
        self._set_busy(True, self._btn_sample, "测试音频中…")
        self._lbl_latency.configure(text="音频测试中… 请让模拟器持续发声 2 秒", foreground="#1a5fb4")
        threading.Thread(target=self._test_audio_worker, daemon=True).start()

    def _test_audio_worker(self):
        try:
            from piano_tool.latency_probe import test_audio_level
            dev = self._get_selected_device()
            loopback = self._get_selected_loopback()
            rms, name = test_audio_level(device=dev, loopback=loopback, duration=2.0)
            self._q.put(("log", f"音频测试：最大电平 {rms:.6f}（设备：{name}）。"
                               f"若数值接近 0，说明没抓到模拟器声音，需要设置回环/立体声混音。"))
            self._q.put(("test_audio_done", (rms, name)))
        except Exception as exc:  # noqa: BLE001
            self._q.put(("log", f"✗ 音频测试失败：{exc}"))
            self._q.put(("test_audio_done", (0.0, None)))

    def _on_q_test_audio_done(self, payload):
        rms, name = payload
        self._set_busy(False, self._btn_sample, "▶ 采集每键音色")
        if rms < 1e-4:
            self._lbl_latency.configure(
                text=f"音频电平过低（{rms:.6f}），未捕获到声音。请检查回环设备/音量/浏览器音频输出设置。",
                foreground="#c0392b")
        else:
            self._lbl_latency.configure(
                text=f"✓ 检测到声音输入（{rms:.6f}，设备：{name}），可以开始采样",
                foreground="#1a7a1a")

    def _on_sample_latency(self):
        if not Path(C.calibration_path).exists():
            messagebox.showwarning("提示", "尚未校准，请先完成「③ 四点校准」")
            return
        if self._busy:
            return
        self._clear_latency_text()
        self._set_busy(True, self._btn_sample, "采样中…")
        if bool(self._var_keyopt.get()):
            self._lbl_latency.configure(
                text="准备中… 马上会倒计时 3 秒，请把模拟器/游戏切到前台",
                foreground="#1a5fb4")
        else:
            self._lbl_latency.configure(
                text="采样中… 请保持模拟器在前、声音开启（每 1.25 秒按一个键，整轮约 30 秒）",
                foreground="#1a5fb4")
        self.iconify()
        threading.Thread(target=self._latency_worker, daemon=True).start()

    def _latency_worker(self):
        try:
            from piano_tool.calibrate_latency import (
                load_latency_profile, sample_all_keys,
                save_latency_profile,
            )
            from piano_tool.latency_probe import AudioMonitor
            from piano_tool.keyboard_play import (
                INPUT_MODE_KEYBOARD,
                INPUT_MODE_SCREEN,
                resolve_calibration,
                tap_key,
            )

            mode = (INPUT_MODE_KEYBOARD if bool(self._var_keyopt.get())
                    else INPUT_MODE_SCREEN)
            cal = resolve_calibration(mode, C.calibration_path)
            keyboard_mode = mode == INPUT_MODE_KEYBOARD
            try:
                monitor = AudioMonitor(
                    device=self._get_selected_device(),
                    loopback=self._get_selected_loopback(),
                )
            except Exception as exc:  # noqa: BLE001
                self._q.put(("log", f"✗ 音频设备不可用，无法自动采样：{exc}。可改用手动模式或确认声卡设置。"))
                self._q.put(("latency_done", None))
                return
            self._q.put(("log", f"开始逐键音色采集（监听设备：{monitor.device_name}）"
                                "—— 延迟仅参考，播放不逐键补偿"))

            # ---- 采样前：先让设备证明「我能收到声音」，听不到就自己换一块 ----
            # 「录屏软件能录、我们录不到」几乎都是同一个原因：
            # **程序听的那块输出设备，不是游戏正在出声的那块**（蓝牙 / HDMI 场景下
            # 系统里常常有好几块输出，下拉框里的选择可能早就失效了）。
            # 所以这里不信下拉框，先开流听 1 秒，按实测电平说话。
            def _open_and_probe():
                monitor.open()
                try:
                    return monitor.probe(seconds=1.0)
                except Exception:  # noqa: BLE001  自检失败按「没数据」处理
                    logger.warning("设备自检失败", exc_info=True)
                    return {"blocks": 0, "peak": 0.0, "device": monitor.device_name}

            try:
                info = _open_and_probe()
            except Exception as exc:  # noqa: BLE001
                self._q.put(("log", f"✗ 打不开音频设备：{exc}"))
                self._q.put(("latency_done", None))
                return

            self._q.put(("log",
                         f"设备自检：{monitor.device_name} → {info.get('blocks', 0)} 块数据，"
                         f"峰值 {info.get('peak', 0.0):.5f}"))

            if info.get("blocks", 0) == 0 or info.get("peak", 0.0) <= 0.0:
                self._q.put(("log",
                             "⚠ 这块设备听不到任何声音（不是音量小，是根本没有数据进来）。\n"
                             "   正在自动找「此刻真的在出声」的那一块…"))
                try:
                    monitor.close()
                except Exception:  # noqa: BLE001
                    pass
                res = []
                try:
                    from piano_tool.latency_probe import probe_loopback_levels

                    loops = [d for d in self._audio_devices if d.get("loopback")]
                    res = probe_loopback_levels(loops, seconds=0.6) if loops else []
                except Exception as exc:  # noqa: BLE001
                    self._q.put(("log", f"（自动探测失败：{exc}）"))
                best = max(res, key=lambda r: float(r.get("peak") or 0.0), default=None)

                if best and float(best.get("peak") or 0.0) > 1e-5:
                    self._q.put(("log",
                                 f"✓ 换到有声音的设备：{best.get('name')}"
                                 f"（峰值 {float(best.get('peak') or 0):.4f}）"))
                    try:
                        monitor = AudioMonitor(device=int(best["index"]), loopback=True)
                        monitor._collect = True
                        info = _open_and_probe()
                    except Exception as exc:  # noqa: BLE001
                        self._q.put(("log", f"✗ 换到那块设备后仍然打不开：{exc}"))
                        self._q.put(("latency_done", None))
                        return
                    if info.get("blocks", 0) == 0:
                        self._q.put(("log", "✗ 换过去之后依然收不到数据，已中止。"))
                        self._q.put(("latency_done", None))
                        return
                elif keyboard_mode:
                    # 键盘模式要**按键才会响**，这 1 秒没声音不代表设备不对，
                    # 重新开流继续（真有问题会在第一键后暴露）。
                    self._q.put(("log",
                                 "⚠ 这 1 秒内所有设备都没声音 —— 键盘模式可能只是还没按键，"
                                 "先继续；如果第一个键之后仍然采不到，会立刻停。"))
                    try:
                        monitor = AudioMonitor(device=self._get_selected_device(),
                                               loopback=self._get_selected_loopback())
                        monitor._collect = True
                        monitor.open()
                    except Exception as exc:  # noqa: BLE001
                        self._q.put(("log", f"✗ 重新打开原设备失败：{exc}"))
                        self._q.put(("latency_done", None))
                        return
                else:
                    self._q.put(("log",
                                 "✗ 所有回环设备都是静音，已中止（免得白测 21 个键）。\n"
                                 "   请先确认：① 游戏/模拟器**现在正在出声**；\n"
                                 "   ② 任务栏音量图标显示的输出设备就是它；③ 那块设备没被静音。\n"
                                 "   然后用「🎯 找正在出声的」重选设备再来一次。"))
                    self._q.put(("latency_done", None))
                    return
            extra: dict = {"key_fn": None}
            if keyboard_mode:
                # ---- 键盘模式专属适配（鼠标模式不需要这些）----
                # ① 按下与抬起之间要留时间：游戏/模拟器对连按有去抖，
                #    `down` 紧跟 `up`（0ms）会被当成「没按」→ 那个键永远采不到。
                # ② 键盘事件只对**前台窗口**生效：先给 3 秒让用户切到模拟器，
                #    否则开头几个键全被吞，看起来就是「没采完」。
                extra["key_fn"] = (
                    lambda kk: tap_key(kk, upper=bool(self._var_upper.get()),
                                       hold_ms=80))
                self._q.put(("log", "键盘模式：请在 3 秒内把模拟器/游戏切到前台，"
                                    "倒计时结束后按固定节拍逐个按键：每个键按住 80 毫秒，间隔 1.25 秒（留时间录音和收尾），"
                                    "整轮约 30 秒；中途别切窗口。"))
                for left in (3, 2, 1):
                    self._q.put(("latency_countdown", left))
                    time.sleep(1.0)
                self._q.put(("latency_countdown", 0))
                try:
                    from piano_tool.focus_guard import (
                        get_foreground, load_target, make_checker,
                    )

                    target = load_target()
                    checker = (make_checker(target,
                                            self._var_focus_word.get().strip())
                               if target else None)
                    if checker is not None:
                        ok_focus, fg_title = checker()
                    else:
                        ok_focus, fg_title = None, get_foreground()[1]
                    if ok_focus is False:
                        self._q.put(("log",
                                     f"⚠ 当前前台是「{fg_title}」，不是目标窗口 —— "
                                     "按键很可能全部被吞。建议中止后重新点「采集每键音色」。"))
                    else:
                        self._q.put(("log", f"前台窗口：{fg_title}，开始按键采样…"))
                except Exception as exc:  # noqa: BLE001  焦点检查失败不该拦住采样
                    self._q.put(("log", f"（焦点检查跳过：{exc}）"))
            try:
                result = sample_all_keys(
                    cal, monitor=monitor,
                    on_each=lambda k, lat: self._q.put(("latency_progress", (k, lat))),
                    sample_dir=C.data_dir / "key_samples",   # 顺手录下每个键的音色（编辑器「模拟音」用）
                    **extra,
                )
            finally:
                # 流是这里提前开的（自检要用），sample_all_keys 不会替我们关
                try:
                    monitor.close()
                except Exception:  # noqa: BLE001
                    pass
            out = save_latency_profile(result, C.latency_profile_path, device_name=monitor.device_name)
            prof = load_latency_profile(out)
            # 逐键延迟补偿已停用：这份文件的延迟值只作记录/参考，
            # 真正有用的是同时采集到 data/key_samples 里的每键音色。
            self._latency_lead = {}
            none_keys = [k for k, v in result.items() if v is None]
            valid = [v for v in result.values() if v is not None]
            if len(valid) >= 4:
                lo, hi = min(valid), max(valid)
                med = sorted(valid)[len(valid) // 2]
                if hi - lo > 0.25 and hi > med * 2:
                    self._q.put(("log",
                                 f"ℹ 这个设备的延迟读数抖动较大（{lo * 1000:.0f}~{hi * 1000:.0f} ms）——"
                                 "不影响音色采集；延迟只作参考，播放已改用统一提前量。"))
            if none_keys:
                hint = ("键盘模式常见原因：这几个键游戏里没绑定、或按键被吞"
                        "（采样期间切了窗口 / 游戏没在前台）。"
                        if keyboard_mode else
                        "请检查音量/回环设备是否选对。")
                self._q.put(("log",
                             f"⚠ 有 {len(none_keys)} 个键没采到声音（{', '.join(none_keys)}）——"
                             f"编辑器「模拟音」里这几个键会用默认音；不影响播放。{hint}"))
            self._q.put(("log", f"✓ 音色采集完成，已保存到 {C.latency_profile_path.name}"
                               f"（延迟均值 {prof.get('mean_ms') if prof else '—'} ms，仅参考；"
                               f"{len(valid)} 个键有音色）"))
            self._q.put(("latency_done", result))
        except Exception as exc:  # noqa: BLE001
            # 只记一行原因：开流失败时 RuntimeError 里已经带了具体对策，
            # 再打一整段 traceback 反而把有用信息埋掉
            logger.error("采样失败：%s", exc)
            self._q.put(("log", f"✗ 采样失败：{exc}"))
            self._q.put(("latency_done", None))

    def _on_q_latency_countdown(self, left):
        """键盘模式采样前的 3-2-1 倒计时（给用户时间切到模拟器）。"""
        if left:
            self._lbl_latency.configure(
                text=f"⏳ {left} 秒后开始按键采样 —— 请把模拟器/游戏切到前台",
                foreground="#b58900")
        else:
            self._lbl_latency.configure(
                text="采样中…（键盘模式：每 1.25 秒一个键，请勿切换窗口，否则按键会被吞）",
                foreground="#1a5fb4")

    def _on_q_latency_progress(self, payload):
        k, lat = payload
        line = f"{k}: {('%.1f ms' % (lat * 1000)) if lat is not None else '未检测到（跳过）'}\n"
        self._lat_text.configure(state="normal")
        self._lat_text.insert("end", line)
        self._lat_text.configure(state="disabled")
        self._lat_text.see("end")

    def _on_q_latency_done(self, payload):
        self._busy = False
        self._set_busy(False, self._btn_sample, "▶ 采集每键音色")
        try:
            self.deiconify()
        except Exception:  # noqa: BLE001
            pass
        if payload:
            valid = [v for v in payload.values() if v is not None]
            total = len(payload)
            if valid:
                mean_ms = sum(valid) / len(valid) * 1000
                self._lbl_latency.configure(
                    text=f"✓ 样本库就绪：{len(valid)}/{total} 键有效，均值 {mean_ms:.1f} ms",
                    foreground="#1a7a1a")
            else:
                self._lbl_latency.configure(
                    text=f"✗ {total} 个键都未检测到声音。请先用「测试音频」确认声音是否进入本程序。",
                    foreground="#c0392b")
        else:
            self._lbl_latency.configure(text="采样未完成，可重试或检查音频设备", foreground="#c0392b")

    def _on_save_manual_latency(self):
        if not Path(C.calibration_path).exists():
            messagebox.showwarning("提示", "尚未校准，请先完成「③ 四点校准」")
            return
        try:
            from piano_tool.calibrate_latency import (build_profile_from_constant,
                                                       load_latency_profile)
            from piano_tool.player import load_calibration

            cal = load_calibration(C.calibration_path)
            ms = float(self._var_manual_latency.get())
            out = build_profile_from_constant(ms, cal, C.latency_profile_path)
            prof = load_latency_profile(out)
            # 仅作为记录/参考；播放不逐键补偿
            self._latency_lead = {}
            self._clear_latency_text()
            self._lat_text.configure(state="normal")
            for k in sorted(prof["latencies"].keys()):
                self._lat_text.insert("end", f"{k}: {prof['latencies'][k]} ms（统一记录）\n")
            self._lat_text.configure(state="disabled")
            self._lbl_latency.configure(
                text=f"✓ 已保存统一延迟记录：{ms:.0f} ms（仅参考，播放不逐键补偿）",
                foreground="#1a7a1a")
            self.log(f"✓ 统一延迟记录已保存：{out}（仅参考，播放不逐键补偿）")
        except Exception as exc:  # noqa: BLE001
            logger.exception("manual latency save failed")
            messagebox.showerror("错误", f"保存手动样本失败：{exc}")

    def _frame_play(self):
        f = ttk.Frame(self._nb, padding=16)
        ttk.Label(f, text="开始前：把浏览器模拟器页面放在最前、别遮挡；\n"
                         "紧急停止：把鼠标甩到屏幕左上角（FAILSAFE）。\n"
                         "③ 选了「② 键盘映射对应」时：播放的是键盘按键（Q~U / A~J / Z~M），"
                         "不用鼠标点；弹得闷/没声就勾下面的「发送大写」。",
                  wraplength=900, justify="left").grid(row=0, column=0, columnspan=4, sticky="w")

        self._var_dry = tk.BooleanVar(value=False)   # 默认真实点击（试运行需手动勾选）
        self._var_upper = tk.BooleanVar(value=False)  # 键盘方式：发送大写字母
        self._var_speed = tk.DoubleVar(value=1.0)
        self._var_delay = tk.IntVar(value=20)
        self._var_countdown = tk.IntVar(value=3)
        self._var_latency = tk.IntVar(value=0)       # 提前量(ms)，抵消点击→发声延迟
        self._var_hold = tk.BooleanVar(value=True)    # 按音符时值按住（还原长短）
        self._var_holdmin = tk.IntVar(value=30)       # 最小按住(ms)
        self._var_backing = tk.BooleanVar(value=False)  # 同时播放伴奏
        self._var_backing_vol = tk.IntVar(value=60)   # 伴奏音量（百分比）
        # 伴奏来源：纯伴奏（分离） / 原曲（含人声）。存的是下拉框里那句文案，
        # 读的时候用 backing_track.normalize_source 认关键词，改文案不会弄坏旧预设。
        self._var_backing_src = tk.StringVar(value=backing_track.SOURCE_LABELS[
            backing_track.SOURCE_STEM])
        self._var_testkey = tk.StringVar()

        ttk.Checkbutton(f, text="试运行（只打印计划，不真实点击）",
                        variable=self._var_dry,
                        command=self._on_toggle_dry).grid(row=1, column=0, sticky="w", pady=(10, 2))
        self._lbl_mode = ttk.Label(f, text="", foreground="#c0392b")
        self._lbl_mode.grid(row=1, column=1, columnspan=4, sticky="w", pady=(10, 2))
        # 这一行以前和「试运行」同在 row=1/column=0，两个 widget 抢同一个 grid 格
        # 会静默互相压盖（试运行复选框会被顶掉），所以必须独占一行。
        ttk.Checkbutton(f, text="键盘方式下发送大写（Shift+字母）：模拟器只认大写字母时才勾它",
                        variable=self._var_upper).grid(row=2, column=0, columnspan=4, sticky="w", pady=(0, 2))
        ttk.Label(f, text="倍速：").grid(row=3, column=0, sticky="e")
        ttk.Spinbox(f, from_=0.25, to=4.0, increment=0.25, width=6,
                    textvariable=self._var_speed).grid(row=3, column=1, sticky="w")
        ttk.Label(f, text="点击间隔(ms)：").grid(row=3, column=2, sticky="e", padx=(16, 0))
        ttk.Spinbox(f, from_=0, to=500, increment=10, width=6,
                    textvariable=self._var_delay).grid(row=3, column=3, sticky="w")
        ttk.Label(f, text="开始倒计时(秒)：").grid(row=4, column=0, sticky="e", pady=(8, 0))
        ttk.Spinbox(f, from_=0, to=30, increment=1, width=6,
                    textvariable=self._var_countdown).grid(row=4, column=1, sticky="w", pady=(8, 0))
        ttk.Label(f, text="提前量(ms)：").grid(row=4, column=2, sticky="e", padx=(16, 0), pady=(8, 0))
        ttk.Spinbox(f, from_=0, to=500, increment=10, width=6,
                    textvariable=self._var_latency).grid(row=4, column=3, sticky="w", pady=(8, 0))
        ttk.Label(f, text="提前量：点击比节拍提前触发，抵消「点击→发声」的延迟，连音更跟手。",
                  foreground="#666").grid(row=5, column=0, columnspan=4, sticky="w")
        # 按住时值（还原音符长短/节奏）
        ttk.Checkbutton(f, text="按音符时值按住（还原长短/节奏）",
                        variable=self._var_hold).grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(f, text="最小按住(ms)：").grid(row=6, column=2, sticky="e", pady=(8, 0))
        ttk.Spinbox(f, from_=5, to=300, increment=5, width=6,
                    textvariable=self._var_holdmin).grid(row=6, column=3, sticky="w", pady=(8, 0))

        # 同时放伴奏：边弹边跟原曲对比。放什么由下面「伴奏来源」决定：
        # 纯伴奏（demucs 分离出的 no_vocals.wav）或原曲本身（① 页那首歌）。
        ttk.Checkbutton(f, text="同时播放伴奏（对比用）",
                        variable=self._var_backing).grid(
                            row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(f, text="伴奏音量(%)：").grid(row=7, column=2, sticky="e", pady=(8, 0))
        ttk.Spinbox(f, from_=0, to=150, increment=5, width=6,
                    textvariable=self._var_backing_vol,
                    command=self._on_backing_vol).grid(row=7, column=3, sticky="w", pady=(8, 0))
        ttk.Label(f, text="伴奏来源：").grid(row=8, column=0, sticky="e", pady=(4, 0))
        self._combo_backing_src = ttk.Combobox(
            f, textvariable=self._var_backing_src, state="readonly", width=32,
            values=[backing_track.SOURCE_LABELS[backing_track.SOURCE_STEM],
                    backing_track.SOURCE_LABELS[backing_track.SOURCE_ORIGINAL]])
        self._combo_backing_src.grid(row=8, column=1, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(f, text="纯伴奏要先在 ① 页勾「先人声分离再转写」跑一次；原曲直接用你选的那首歌"
                          "（人声会盖住琴声，判断不准时可切回纯伴奏）。音量播放中改立即生效。",
                  foreground="#666", wraplength=900, justify="left").grid(
                      row=9, column=0, columnspan=4, sticky="w", pady=(0, 2))

        row = ttk.Frame(f)
        row.grid(row=10, column=0, columnspan=4, sticky="w", pady=14)
        self._btn_play = ttk.Button(row, text="▶ 开始播放", command=self._on_play)
        self._btn_play.pack(side="left")
        # 播放的同时打开浏览器编辑器：一边弹一边改谱，改完「保存到项目」立即生效
        self._btn_play_edit = ttk.Button(
            row, text="▶ 播放 + 同时编辑", command=self._on_play_with_editor)
        self._btn_play_edit.pack(side="left", padx=(6, 0))
        self._btn_stop = ttk.Button(row, text="■ 停止", command=self._on_stop, state="disabled")
        self._btn_stop.pack(side="left", padx=8)
        ttk.Label(row, text="（播放中快速三按鼠标右键 = 暂停）",
                  foreground="#666").pack(side="left", padx=4)
        ttk.Separator(row, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Label(row, text="自检键：").pack(side="left")
        self._combo_testkey = ttk.Combobox(
            row, textvariable=self._var_testkey, width=8, state="readonly",
            values=[str(k["key"]) for k in instruments.active().keys])
        self._combo_testkey.pack(side="left")
        if instruments.active().keys:
            self._combo_testkey.current(0)
        self._btn_test = ttk.Button(row, text="🎯 点击自检（只点一下）",
                                    command=self._on_self_test)
        self._btn_test.pack(side="left", padx=8)

        self._lbl_play = ttk.Label(f, text="（空闲）", foreground="#666")
        self._lbl_play.grid(row=11, column=0, columnspan=4, sticky="w")
        ttk.Label(f, text="快捷键 F7：开始 / 停止（全局热键，本窗口最小化时同样有效）",
                  foreground="#1a5fb4").grid(row=12, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # ---- 焦点守卫 ----
        # 注入按键只对「前台窗口」生效。焦点跳到别的窗口时目标程序收不到，
        # 但按键还在不停地注入前台窗口（网页快捷键/输入框被乱按）——这就是那个 bug。
        fg = ttk.LabelFrame(f, text=" 焦点守卫（切走到别的窗口时自动停，别再乱按） ", padding=8)
        fg.grid(row=13, column=0, columnspan=4, sticky="we", pady=(12, 0))
        self._var_focus_guard = tk.BooleanVar(value=False)
        self._var_focus_word = tk.StringVar()
        self._var_focus_lost = tk.StringVar(value="wait")
        ttk.Checkbutton(fg, text="启用焦点守卫（只在目标窗口处于前台时弹奏）",
                        variable=self._var_focus_guard,
                        command=self._update_focus_hint).pack(anchor="w")
        frow = ttk.Frame(fg)
        frow.pack(fill="x", pady=(6, 2))
        ttk.Label(frow, text="目标窗口：").pack(side="left")
        ttk.Entry(frow, textvariable=self._var_focus_word, width=36).pack(side="left", padx=(2, 8))
        ttk.Button(frow, text="① 抓取当前窗口", command=self._on_capture_focus).pack(side="left")
        ttk.Button(frow, text="🎯 瞄准镜选窗", command=self._on_pick_window).pack(side="left", padx=(6, 0))
        ttk.Button(frow, text="② 切到该窗口", command=self._on_activate_focus).pack(side="left", padx=(6, 0))
        ttk.Button(frow, text="🩺 目标窗口体检", command=self._on_check_target).pack(side="left", padx=(6, 0))
        lrow = ttk.Frame(fg)
        lrow.pack(fill="x")
        ttk.Label(lrow, text="焦点跑掉时：").pack(side="left")
        ttk.Radiobutton(lrow, text="暂停等待（切回目标窗口自动继续）",
                        value="wait", variable=self._var_focus_lost).pack(side="left")
        ttk.Radiobutton(lrow, text="静音跳过", value="skip",
                        variable=self._var_focus_lost).pack(side="left", padx=(12, 0))
        ttk.Radiobutton(lrow, text="直接停止", value="stop",
                        variable=self._var_focus_lost).pack(side="left", padx=(12, 0))
        self._lbl_focus = ttk.Label(fg, text="", wraplength=880, justify="left",
                                    foreground="#1a5fb4")
        self._lbl_focus.pack(anchor="w", pady=(6, 0))
        ttk.Label(fg,
                  text="说明：目标窗口由你自己选定，可以是浏览器模拟器或任意程序，软件不内置、不固定任何窗口。"
                       "选窗两种方式：点「🎯 瞄准镜选窗」把准星移到目标窗口上松手，或点「① 抓取当前窗口」后 6 秒内切过去。"
                       "软件只识别窗口标题与进程号，不读取画面、不修改任何内容；你切走后即按上面的选择自动暂停/跳过/停止。",
                  wraplength=880, justify="left", foreground="#888").pack(anchor="w", pady=(4, 0))
        self._update_focus_hint()
        self._on_toggle_dry()
        return f

    # ---- 焦点守卫 ----
    def _update_focus_hint(self):
        """刷新焦点守卫下方的状态说明。"""
        if not hasattr(self, "_lbl_focus"):
            return
        target = getattr(self, "_focus_target", None)
        if not bool(self._var_focus_guard.get()):
            self._lbl_focus.configure(
                text="当前：关闭（焦点跑到浏览器时按键仍会照发给浏览器 —— 就是那个 bug）")
            return
        if target is not None and not target.is_empty():
            name = target.title or target.proc or f"pid={target.pid}"
            self._lbl_focus.configure(
                text=f"目标窗口：{name}（pid={target.pid or '?'}）。"
                     f"失焦时：{self._focus_lost_text()}")
            return
        kw = self._var_focus_word.get().strip()
        if kw:
            self._lbl_focus.configure(
                text=f"按标题关键字匹配：{kw}。失焦时：{self._focus_lost_text()}（建议先点「① 抓取当前窗口」锁定）")
        else:
            self._lbl_focus.configure(
                text="还没指定目标窗口：先切到你要弹奏的窗口（浏览器模拟器或任意程序），"
                     "再点「① 抓取当前窗口」（6 秒内切过去即可），或手填窗口标题关键字。")

    def _focus_lost_text(self) -> str:
        return {"wait": "暂停等待，切回目标窗口自动继续",
                "skip": "静音跳过失焦期间的事件",
                "stop": "直接停止演奏"}.get(self._var_focus_lost.get(), "暂停等待")

    def _on_capture_focus(self):
        """点一下之后 6 秒内切到要弹奏的窗口，就把它记成目标窗口。"""
        import os

        from piano_tool.focus_guard import FocusTarget, get_foreground, save_target

        my_pid = os.getpid()
        self.log("焦点守卫：6 秒内切到你要弹奏的窗口（浏览器模拟器或任意程序），我会把它记成目标窗口…")

        def work():
            for _ in range(20):
                time.sleep(0.3)
                pid, title, proc = get_foreground()
                if pid and pid != my_pid:
                    target = FocusTarget(pid=pid, title=title, proc=proc)
                    save_target(target)
                    self._q.put(("focus_captured", (pid, title, proc)))
                    return
            self._q.put(("focus_captured", None))

        threading.Thread(target=work, daemon=True).start()

    def _on_q_focus_captured(self, payload):
        if not payload:
            self.log("✗ 没抓到别的窗口（6 秒内请切到你要弹奏的窗口再试）")
            self._update_focus_hint()
            return
        self._apply_focus_target(*payload)

    def _apply_focus_target(self, pid, title, proc):
        """两条选窗路径（6 秒倒计时抓取 / 瞄准镜）共用的落库与界面更新。"""
        from piano_tool.focus_guard import FocusTarget, save_target

        target = FocusTarget(pid=pid, title=title, proc=proc)
        save_target(target)
        self._focus_target = target
        self._var_focus_guard.set(True)
        self.log(f"✓ 已锁定目标窗口：{title or proc or pid}（pid={pid}）")
        self._update_focus_hint()

    def _on_pick_window(self):
        """打开瞄准镜：把准星拖/移到目标窗口上松手即可锁定。"""
        _CrosshairPicker(self, on_picked=self._on_window_picked).show()

    def _on_window_picked(self, target):
        self._apply_focus_target(target.pid, target.title, target.proc)

    def _on_activate_focus(self):
        from piano_tool.focus_guard import activate

        target = getattr(self, "_focus_target", None)
        if target is None or target.is_empty():
            messagebox.showwarning("提示", "还没锁定目标窗口，请先点「① 抓取当前窗口」")
            return
        if activate(target):
            self.log(f"已尝试切到：{target.title or target.proc}")
        else:
            messagebox.showwarning("提示", "没能切过去，请手动点一下目标窗口")

    def _on_check_target(self):
        """🩺 目标窗口体检：用标准只读接口查出「为什么这台机器弹不进游戏」。

        检查项：目标进程权限级别（UIPI 是否拦截）、系统 DPI 缩放、输入注入后端。
        不读取、不修改目标程序的任何内容。
        """
        from piano_tool.focus_guard import diagnose_target

        target = getattr(self, "_focus_target", None)
        kw = self._var_focus_word.get().strip()
        try:
            items = diagnose_target(target, kw)
        except Exception as exc:  # noqa: BLE001  体检本身不该把界面搞崩
            messagebox.showwarning("目标窗口体检", f"体检失败：{exc}")
            return

        icon = {"ok": "✓ ", "warn": "⚠ ", "info": "· "}
        lines = [icon.get(level, "· ") + text for level, text in items]
        for line in lines:
            self.log(line)
        messagebox.showinfo("目标窗口体检", "\n\n".join(lines) or "没有可报告的问题")

    def _on_toggle_dry(self):
        if self._var_dry.get():
            self._lbl_mode.configure(text="当前：试运行（不会点击）", foreground="#666")
        else:
            self._lbl_mode.configure(
                text="当前：真实点击模式 —— 开始后本窗口会自动最小化，请把浏览器模拟器置于最前",
                foreground="#c0392b",
            )

    def _on_backing_vol(self):
        """伴奏音量改动：立即作用到正在播的那条伴奏上（不用等下次播放）。"""
        try:
            v = int(float(self._var_backing_vol.get()))
        except (TypeError, ValueError):
            return          # Spinbox 被清空时会写进空串，忽略即可
        v = max(0, min(150, v))
        if int(self._var_backing_vol.get() or 0) != v:
            self._var_backing_vol.set(v)
        if self._backing is not None:
            self._backing.volume = v / 100.0

    # --------------------------------------------------------------- 日志
    def log(self, msg: str):
        if not hasattr(self, "log_text"):
            # 界面还没建完（Notebook 里各页在日志控件之前构造）：
            # 直接写会 AttributeError，被上层 except 吃掉还会误报「枚举设备失败」。
            self.after(200, lambda m=msg: self.log(m))
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_q_log(self, msg):
        """后台线程的日志（队列里的 "log"）—— 没这个入口日志会被静默丢掉。"""
        self.log(str(msg))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                handler = getattr(self, "_on_q_" + kind, None)
                if handler:
                    handler(payload)
        except queue.Empty:
            pass
        self.after(120, self._poll_queue)

    # ------------------------------------------------------ 全局热键 F7
    def _setup_hotkey(self):
        """注册全局 F7 热键（窗口最小化/失焦时也能开始与停止播放）。"""
        try:
            import keyboard
        except Exception as exc:  # noqa: BLE001
            self._hotkey = None
            self.log(f"✗ 未安装 keyboard 库，F7 不可用：{exc}")
            self.log("  安装：.venv\\Scripts\\python.exe -m pip install keyboard")
            return
        try:
            keyboard.add_hotkey("f7", self._on_hotkey_f7, suppress=False)
            self._hotkey = keyboard
            self.log("✓ 已注册全局热键 F7：开始 / 停止播放（任何窗口下都有效）")
        except Exception as exc:  # noqa: BLE001
            self._hotkey = None
            self.log(f"✗ F7 热键注册失败：{exc}")

    def _on_hotkey_f7(self):
        """keyboard 库的回调（在其自己的线程里执行）→ 转交 Tk 主线程。"""
        self._q.put(("hotkey_f7", None))

    def _on_q_hotkey_f7(self, _payload):
        """F7：播放中 → 停止；暂停中 → 继续；空闲 → 开始。"""
        if self._play_paused:
            self.log("F7 → 继续播放")
            self._resume_playback()
        elif self._playing:
            self.log("F7 → 停止播放")
            self._on_stop()
        else:
            if self._busy:
                self.log("F7 → 正忙（转写/映射中），忽略")
                return
            self.log("F7 → 开始播放")
            self._on_play()

    # --------------------------------------------------------- ① 音频→MIDI
    def _browse_audio(self):
        p = filedialog.askopenfilename(
            title="选择音频", filetypes=[("音频", "*.mp3 *.wav *.flac *.m4a *.ogg"), ("所有文件", "*.*")]
        )
        if p:
            self._var_audio.set(p)

    def _vocalsep_text(self) -> str:
        """「先人声分离」复选框文案，跟着 demucs 是否可用变。"""
        if self._demucs_ok:
            return "先人声分离再转写（带伴奏的歌推荐；首次使用自动下载约 160MB 模型）"
        return "先人声分离再转写（首次勾选会下载 demucs，约 500MB~1GB）"

    def _on_vocalsep_toggle(self):
        """勾选「先人声分离」时才按需安装 demucs，不在启动时自动下载。"""
        if not self._var_vocalsep.get() or self._demucs_ok:
            return
        try:
            from piano_tool.vocal_separation import is_demucs_available

            if is_demucs_available():
                self._demucs_ok = True
                self._cb_vocalsep.configure(text=self._vocalsep_text())
                return
        except Exception:  # noqa: BLE001
            pass

        if not messagebox.askyesno(
            "需要下载人声分离组件",
            "人声分离需要 demucs（会连带安装 PyTorch，约 500MB~1GB）。\n\n"
            "现在就下载安装吗？\n"
            "安装过程显示在下方日志里；即使失败也不影响转写，可以直接用原曲。",
            parent=self,
        ):
            self._var_vocalsep.set(False)
            return
        self._start_demucs_install()

    def _start_demucs_install(self):
        """后台安装 demucs，日志实时转到界面（不阻塞主界面）。"""
        self._demucs_installing = True
        self._demucs_t0 = time.time()
        self._cb_vocalsep.configure(
            state="disabled", text="正在下载 demucs…（进度见下方日志）")
        self._btn_convert.configure(state="disabled")
        self.log("开始下载 demucs（约 500MB~1GB，首次较久，请保持网络畅通）…")
        self.log("  日志会实时出现在这里；大文件下载中途没有新行也是正常的，"
                 "每 10 秒会提示一次「仍在下载」。")
        self.after(10000, self._demucs_heartbeat)
        threading.Thread(target=self._demucs_install_worker, daemon=True).start()

    def _demucs_heartbeat(self):
        """下载期间每 10 秒报一次等待时长，避免看起来像卡死。"""
        if not self._demucs_installing:
            return
        waited = int(time.time() - self._demucs_t0)
        self.log(f"  仍在下载 demucs / PyTorch…已等待 {waited} 秒")
        self.after(10000, self._demucs_heartbeat)

    def _demucs_install_worker(self):
        """子进程跑 `bootstrap.py optional`：走项目自带的镜像回退。"""
        import re
        import subprocess

        bootstrap = Path(__file__).resolve().parent / "bootstrap.py"
        cmd = [sys.executable, "-u", str(bootstrap), "optional"]
        # 子进程输出走管道，Python 默认按 GBK 编码中文；强制 UTF-8 才能正确显示
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        pct_re = re.compile(r"(\d{1,3})%")
        last_pct, last_pct_at = -10, 0.0
        rc = 1
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                # pip 的进度条用 \r 刷新：按 5% 或 10 秒节流，别把日志刷爆
                m = pct_re.search(line)
                if m and ("%" in line):
                    pct = int(m.group(1))
                    now = time.time()
                    if pct - last_pct < 5 and now - last_pct_at < 10:
                        continue
                    last_pct, last_pct_at = pct, now
                print(line, flush=True)          # 同时显示在启动.bat 的控制台
                self._q.put(("log", "  " + line))
            rc = proc.wait()
        except Exception as exc:  # noqa: BLE001
            self._q.put(("log", f"✗ demucs 安装失败：{exc}"))

        ok = False
        if rc == 0:
            try:
                from piano_tool.vocal_separation import is_demucs_available

                ok = is_demucs_available()
            except Exception:  # noqa: BLE001
                ok = False
        self._q.put(("demucs_install_done", (rc, ok)))

    def _on_q_demucs_install_done(self, payload):
        rc, ok = payload
        self._demucs_installing = False
        self._demucs_ok = bool(ok)
        self._cb_vocalsep.configure(state="normal", text=self._vocalsep_text())
        self._btn_convert.configure(state="normal" if self._bp_ok else "disabled")
        if ok:
            self._var_vocalsep.set(True)
            self.log("✓ demucs 安装完成，人声分离已可用")
        else:
            self._var_vocalsep.set(False)
            self.log(f"✗ demucs 未装好（退出码 {rc}）。不影响转写；可稍后重试，"
                     "或手动执行：.venv\\Scripts\\python.exe -m pip install demucs")

    def _on_convert(self):
        src = self._var_audio.get().strip()
        if not src or not Path(src).exists():
            messagebox.showwarning("提示", "请先选择存在的音频文件")
            return
        if not self._bp_ok:
            messagebox.showerror("缺少依赖",
                                 "当前 Python 没有 basic_pitch。\n"
                                 "请用项目目录下的 启动.bat 启动，或检查 .venv 依赖是否安装完整。")
            return
        self._set_busy(True, self._btn_convert, "转写中…（人声分离需几分钟，进度看日志）")
        self.log(f"开始转写：{src}")
        # tk 变量不能跨线程读：主线程先读好再传参
        vocal_sep = bool(self._var_vocalsep.get()) and self._demucs_ok
        sep_device = (self._var_sep_device.get() or "auto") if self._demucs_ok else "auto"
        threading.Thread(target=self._convert_worker, args=(src, vocal_sep, sep_device),
                         daemon=True).start()

    def _convert_worker(self, src: str, vocal_sep: bool = False,
                        sep_device: str = "auto"):
        """音频 → MIDI。

        一律走乐器的默认阈值。以前这里会判断「这个音频是不是文本发声合成的语音」
        （语音起音不清晰，要放宽阈值），随 TTS 模块一起去掉了：
        普通音乐走语音阈值会被当成噪声、漏掉真音符。

        vocal_sep: 先用 Demucs 分离人声再转写（主线程读好的布尔值）。
        分离失败不中止——回退整曲转写，让流程继续走。
        sep_device: 保留参数（历史兼容），实际固定用 CPU——分离开销很小，
        程序不再提供 GPU 模式。
        """
        try:
            from piano_tool.audio_to_midi import audio_to_midi

            src_for_transcribe = src
            if vocal_sep:
                try:
                    from piano_tool.vocal_separation import separate_vocals

                    device = "cpu"

                    self._q.put(("log", "先做人声分离（首次使用会自动下载模型，请耐心）…"))
                    vocals = separate_vocals(
                        src, C.output_dir / "separated",
                        device=device,
                        progress=lambda line: self._q.put(("log", f"  {line}")),
                    )
                    src_for_transcribe = str(vocals)
                    self._q.put(("log", f"✓ 人声轨已分离，转写人声：{vocals}"))
                except Exception as exc:  # noqa: BLE001
                    logger.exception("vocal separation failed")
                    self._q.put(("log", f"⚠ 人声分离失败，回退整曲转写：{exc}"))

            # 转写推理（basic_pitch/tensorflow）本身没有进度回调——整个过程
            # 也必须有「活着」的信号，否则用户又会以为卡死了。
            stop_evt = threading.Event()
            t0 = time.time()

            def _beat():
                while not stop_evt.wait(15):
                    self._q.put(("log", f"  转写推理中…已等待 {int(time.time() - t0)} 秒"
                                        f"（普通歌曲约 1~3 分钟，属正常）"))

            hb = threading.Thread(target=_beat, daemon=True)
            hb.start()
            try:
                out = C.output_dir / "song.mid"
                count, out_path = audio_to_midi(src_for_transcribe, out)
            finally:
                stop_evt.set()
            # 登记「这份 MIDI 是用哪个人声轨转写的」——② 页映射同一份
            # MIDI 时按实测基频校准旋律（只对首席女高音生效）。
            # 没走分离（或分离失败回退整曲）就清除旧记录，防止串歌。
            _midi_key = str(Path(out_path).resolve())
            if src_for_transcribe != src:
                self._vocals_for_midi[_midi_key] = str(src_for_transcribe)
            else:
                self._vocals_for_midi.pop(_midi_key, None)
            self._q.put(("log", f"✓ 转写完成：{count} 个音符 -> {out_path}"))
            self._q.put(("convert_done", str(out_path)))
        except Exception as exc:  # noqa: BLE001
            logger.exception("convert failed")
            self._q.put(("log", f"✗ 转写失败：{exc}"))
            # 失败也要发完成消息：恢复按钮，且 payload=None 告诉回调"别改②页路径"
            self._q.put(("convert_done", None))

    # ------------------------------------------------------ 转写完成回调
    def _on_q_convert_done(self, payload):
        self._set_busy(False, self._btn_convert, "开始转写")
        if not payload:
            self.log("（转写未完成，②页「MIDI 文件」路径保持不变）")
            return
        # 用转写 worker 返回的真实产物路径（不再硬编码），并同步给②页
        out_path = str(Path(payload).resolve())
        self._var_midi.set(out_path)
        self.log(f"✓ ②页「MIDI 文件」路径已自动填入：{out_path}")
        # 自动切到②页，直接进入下一步「MIDI→琴谱」
        try:
            self._nb.select(1)
            self._flash_midi_entry()
        except Exception:  # noqa: BLE001
            pass

    def _flash_midi_entry(self, times=4):
        """路径刚自动填入时闪两下高亮，提示用户位置已更新。"""
        entry = getattr(self, "_ent_midi", None)
        if entry is None:
            return
        colors = ["#fff3cd", None] * times

        def _toggle(idx=0):
            if idx >= len(colors):
                return
            color = colors[idx]
            if color:
                entry.configure(highlightbackground=color, highlightcolor=color)
            else:
                entry.configure(highlightbackground="#7a7a7a", highlightcolor="#1f6feb")
            self.after(220, _toggle, idx + 1)

        _toggle()

    # --------------------------------------------------------- ② MIDI→琴谱
    def _browse_midi(self):
        p = filedialog.askopenfilename(title="选择 MIDI", filetypes=[("MIDI", "*.mid *.midi"), ("所有文件", "*.*")])
        if p:
            self._var_midi.set(p)

    def _on_map(self):
        src = self._var_midi.get().strip()
        if not src or not Path(src).exists():
            messagebox.showwarning("提示", "请先选择存在的 MIDI 文件")
            return
        self._set_busy(True, self._btn_map, "映射中…")
        threading.Thread(target=self._map_worker, args=(src,), daemon=True).start()

    def _map_worker(self, src: str):
        try:
            from piano_tool.midi_mapper import map_midi_to_events, write_events

            # 这份 MIDI 如果是「先人声分离再转写」出来的，带上同一首歌的
            # 人声轨做基频校准（只对首席女高音生效；风物之诗琴忽略）。
            audio_path = self._vocals_for_midi.get(str(Path(src).resolve()))
            arrange, layered, layer_mode = self._arrange_params()
            events, stats = map_midi_to_events(
                src,
                auto_transpose=bool(self._var_autotp.get()),
                mono=bool(self._var_mono.get()),
                melody_mode=self._var_melody_mode.get() or "high",
                fold_out_of_range=(self._var_oor.get() == "fold"),
                layered_octave=layered,
                layer_mode=layer_mode,
                contour_black_keys=bool(self._var_contour.get()),
                instrument=self._var_instrument.get(),
                audio_path=audio_path,
                arrange=arrange,
                polyphony=int(self._var_polyphony.get()),
                # 钢琴曲适配：键盘 = 保留同时和弦；鼠标 = 和弦拆琶音
                piano_input_mode=("keyboard" if bool(self._var_keyopt.get())
                                  else "screen"),
                piano_arpeggio_ms=30.0,
            )
            if stats.get("layered"):
                self._q.put(("log", "已开启分层独立八度：" + "；".join(
                    f"第{i+1}层 原始{fmt_range(r['orig_range'])}→{fmt_range(r['mapped_range'])}"
                    f"（带{fmt_range(r['band'])}，偏移{r['shift']:+d}半音）"
                    for i, r in enumerate(stats["layers"]))))
            if stats.get("track_fallback") == "single_track":
                self._q.put(("log",
                             "独立分轨：只有一条 MIDI 音轨，改为整体落位"
                             "（保留原曲音程，不按音高硬切三层）"))
            elif self._var_arrange.get() in ("track", "pitch"):
                _arr = ("独立分轨" if self._var_arrange.get() == "track"
                        else "按音高分三段")
                self._q.put(("log",
                             f"声部整理方式：{_arr}（同时最多 "
                             f"{self._var_polyphony.get()} 音，只减音数、不改节奏）"))
            _ll = stats.get("lyre_layout")
            if _ll and _ll.get("melody_shift"):
                self._q.put(("log",
                             f"原琴风格：旋律下四分位 "
                             f"{_ll.get('before_q25')}→{_ll.get('after_q25')}"
                             f"（整体抬高 {_ll['melody_shift']:+d} 半音），"
                             f"旋律进中/高排、底排留给低音；同键去重 "
                             f"{_ll.get('dropped', 0)}"))
            p = stats.get("piano")
            if p:
                _roles = p.get("roles") or {}
                _key = p.get("key") or {}
                self._q.put(("log",
                             f"钢琴曲适配：移调 {_key.get('shift', 0):+d} 半音"
                             f"（自然音覆盖 {(_key.get('coverage') or 0) * 100:.0f}%）"
                             f" · 旋律 {_roles.get('melody', 0)} / 低音 "
                             f"{_roles.get('bass', 0)} / 和声 {_roles.get('harmony', 0)}"
                             f" · 和声减薄 {p.get('harmony_dropped', 0)}"
                             f" · 半音改写 {p.get('chromatic_snapped', 0)}"
                             f"（{'键盘=同时和弦' if p.get('input_mode') == 'keyboard' else '鼠标=琶音'}）"))
            out = write_events(events, C.output_dir / "events.json")
            self._q.put(("log", f"✓ 映射完成：{stats['total']} 音符 -> {out}"))
            self._q.put(("map_done", stats))
        except Exception as exc:  # noqa: BLE001
            logger.exception("map failed")
            self._q.put(("log", f"✗ 映射失败：{exc}"))
            self._q.put(("map_done", None))

    def _on_q_map_done(self, stats):
        self._set_busy(False, self._btn_map, "生成琴谱")
        if stats:
            rate = stats.get("white_hit_rate")
            inst = instruments.get_instrument(stats.get("instrument"))
            # 音域体检：原始音高范围 → 移调 → 落进当前乐器音域的范围，
            # 对标模拟器「原始范围 / 合并偏移 / 调整后范围」那块面板
            o = fmt_range(stats.get("orig_range"))
            m = fmt_range(stats.get("mapped_range"))
            span = stats.get("white_span") or [inst.lowest_midi, inst.highest_midi]
            out_of_span = ""
            mr = stats.get("mapped_range")
            if mr is not None and (mr[0] < span[0] or mr[1] > span[1]):
                out_of_span = " ⚠ 超出琴键范围"
            lay_txt = ""
            if stats.get("layered"):
                lay_txt = "\n" + " ｜ ".join(
                    f"第{i + 1}层 原始{fmt_range(r['orig_range'])}→{fmt_range(r['mapped_range'])}"
                    f"（带{fmt_range(r['band'])}，偏移{r['shift']:+d}半音，{r['count']}音）"
                    for i, r in enumerate(stats.get("layers") or []))
            contour_txt = (f"（其中走向改编 {stats.get('contour_redirect', 0)}）"
                           if stats.get("contour") else "")
            preset_txt = ""
            if stats.get("monophonic"):
                preset_txt = ("\n人声预设：只取主旋律 ✓ · 分层独立八度 关 ✓ · "
                              "旋律按「保原曲音程」整条落位（不做逐音折叠）")
            art = stats.get("articulation")
            if art:
                preset_txt += (f"\n唱法（按实测发声学写时值）：最短按住 {art['min_hold']*1000:.0f}ms · "
                               f"补足短音 {art['stretched']} · 连唱 {art['legato']} · "
                               f"让位重叠 {art['trimmed']} · 同音并成长音 {art.get('merged_same', 0)} · "
                               f"保留换气 {art['breathe']}")
            soft = stats.get("auto_soften")
            if soft:
                preset_txt += (f"\n按参考谱收窄：降八度 {soft['ceiling_moved']} 个音 · "
                               f"跳进平滑 {soft['leap_smoothed']} 个音"
                               f"（上限 {instruments.midi_name(soft['ceiling'])}、"
                               f"最大跳进 {soft['max_leap']} 半音）")
            vp = stats.get("voice_prep")
            if vp:
                _frag = vp.get("fragments") or {}
                _dp = vp.get("octave_spikes") or {}
                _au = vp.get("audio") or {}
                voice_txt = (f"\n人声稳化：八度纠错 {_dp.get('repaired', 0)} 个音"
                             f" · 合并同音碎片 {_frag.get('joined', 0)}"
                             f" · 删幽灵毛刺 {_frag.get('ghosts', 0)}")
                if _au.get("applied"):
                    voice_txt += (f"\n人声轨校准（覆盖率 "
                                  f"{float(_au.get('coverage', 0)) * 100:.0f}%）："
                                  f"按基频修正八度 {_au.get('octave_fixed', 0)} · "
                                  f"剔除非人声 {_au.get('dropped_outside', 0)}／"
                                  f"毛刺 {_au.get('dropped_mismatch', 0)} · "
                                  f"起点贴字头 {_au.get('onset_snapped', 0)}")
                elif _au.get("skipped_reason"):
                    voice_txt += f"\n人声轨校准已跳过：{_au['skipped_reason']}"
                preset_txt += voice_txt
            if stats.get("track_fallback") == "single_track":
                preset_txt += ("\n单轨 MIDI：没有音轨可拆，已改为整体落位"
                               "（保留原曲音程，不再按音高硬切三层）")
            _lyre = stats.get("lyre_layout")
            if _lyre:
                if _lyre.get("melody_shift"):
                    preset_txt += (
                        f"\n原琴风格：旋律整体抬高 {_lyre['melody_shift']:+d} 半音"
                        f"（下四分位 {_lyre.get('before_q25')}→"
                        f"{_lyre.get('after_q25')}），旋律进中/高排、底排留给低音"
                        f"；同键去重 {_lyre.get('dropped', 0)}")
                else:
                    preset_txt += "\n原琴风格：旋律已在中/高排，无需抬高"
            _piano = stats.get("piano")
            if _piano:
                _roles = _piano.get("roles") or {}
                _key = _piano.get("key") or {}
                _cov = _key.get("coverage")
                _mode_txt = ("键盘=同时和弦"
                             if _piano.get("input_mode") == "keyboard" else "鼠标=琶音")
                preset_txt += (
                    f"\n钢琴曲适配：移调 {_key.get('shift', 0):+d} 半音"
                    f"（自然音覆盖 "
                    f"{('%.0f%%' % (_cov * 100)) if _cov is not None else '—'}）"
                    f" · 旋律 {_roles.get('melody', 0)} / 低音 "
                    f"{_roles.get('bass', 0)} / 和声 {_roles.get('harmony', 0)}"
                    f" · 和声减薄 {_piano.get('harmony_dropped', 0)}"
                    f" · 半音改写 {_piano.get('chromatic_snapped', 0)}"
                    f" · 同键冲突 {_piano.get('collisions', 0)}"
                    f" · 旋律八度纠错 {_piano.get('melody_octave_fixed', 0)}"
                    f"（最多 {_piano.get('max_voices', 0)} 音 · {_mode_txt}）")
                _rf = _piano.get("role_folded") or {}
                if _rf:
                    preset_txt += (
                        f"\n角色归位：低音→底排 {_rf.get('bass', 0)}"
                        f" · 和声→中排 {_rf.get('harmony', 0)}"
                        f" · 旋律→中/顶排 {_rf.get('melody', 0)}")
            oct_txt = ""
            if stats.get("octave_shift"):
                oct_txt = (f"（含八度归位 {stats['octave_shift']:+d}，只换八度、不改调）")
            self._lbl_map_stats.configure(
                text=(f"乐器：{inst.name} · 音域：原始 {o} → 移调 {stats['transpose']:+d} 半音"
                      f"{oct_txt} → 落到 {m}"
                      f"（琴键 {span[0]}~{span[1]}，用了 {stats['keys_used']} 个键）"
                      f"{out_of_span}\n"
                      f"共 {stats['total']} 音符 · 白键命中 "
                      f"{('%.1f%%' % (rate*100)) if rate is not None else '—'} · "
                      f"音高被改动 {stats['black_replaced']}{contour_txt} · "
                      f"八度折叠 {stats['octave_folded']} · "
                      f"超音域丢弃 {stats.get('dropped_range', 0)} · "
                      f"清理毛刺 {stats['dropped']}"
                      f"{preset_txt}"
                      f"{lay_txt}"),
                foreground="#1a7a1a" if not out_of_span else "#c0392b",
            )
            if vp and (vp.get("audio") or {}).get("applied"):
                self.log("✓ 已按①页分离出的人声轨校准旋律"
                         "（修八度错误 / 剔除非人声和毛刺 / 起点贴字头）")
            self.log("琴谱已生成，可直接去「④ 播放」，或点「在浏览器中微调」。")

    def _on_export_notation(self):
        ev_path = C.output_dir / "events.json"
        if not ev_path.exists():
            messagebox.showwarning("提示", "尚未生成 events.json，请先完成「② MIDI→琴谱」")
            return
        try:
            from piano_tool.midi_mapper import export_key_notation
            from piano_tool.player import load_events

            out = C.output_dir / "按键谱.txt"
            out.write_text(export_key_notation(load_events(ev_path)), encoding="utf-8")
            self.log(f"✓ 按键谱已导出：{out}（可用记事本打开，风格同 案例曲电脑按键版.txt）")
            os.startfile(str(out))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("错误", f"导出按键谱失败：{exc}")

    # ---- ② 「人声匹配」：一个汉字一个音 ----
    def _on_vocal_match(self):
        """人声匹配：把已有旋律改成一个音节一个音。

        两种模式：
        · 自动识别 —— 从歌曲音频里切音节（不用写歌词），按时间对齐；
        · 手动歌词 —— 歌词按顺序配给音符，数量对不上时先问用户怎么取舍。
        人声一次只能唱一个字，所以同一个字不能同时占好几个音、也不该在字中间
        再起一次音。所有操作都不偷偷删音，写回前一定先让用户确认。
        """
        ev_path = C.output_dir / "events.json"
        if not ev_path.exists():
            messagebox.showwarning("提示", "尚未生成 events.json，请先完成「② MIDI→琴谱」")
            return
        try:
            from piano_tool.player import load_events

            events = load_events(ev_path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("错误", f"读取琴谱失败：{exc}")
            return
        if not events:
            messagebox.showwarning("提示", "琴谱里没有音符，先生成琴谱再匹配")
            return

        res = self._ask_vocal_match(len(events), events)
        if not res:
            return
        if res.get("mode") == "auto":
            self._vocal_match_auto(events, str(res.get("audio") or ""))
            return

        from piano_tool.lyrics import align_one_char_one_note, parse_lyrics

        chars = parse_lyrics(res["lyrics"])
        if not chars:
            messagebox.showwarning("提示", "没有识别到歌词里的字（标点和空格不占音）")
            return

        n, k = len(events), len(chars)
        policy = "merge"
        if n > k:
            ans = messagebox.askyesnocancel(
                "字数与音符数不一致",
                f"琴谱有 {n} 个音，歌词有 {k} 个字，多了 {n - k} 个音。\n\n"
                "选「是」：把多出来的音并进相邻的字（推荐，旋律一个音都不丢）\n"
                "选「否」：丢掉多余的音（丢掉时值最短的）\n"
                "选「取消」：不处理",
            )
            if ans is None:
                return
            policy = "merge" if ans else "drop"
        elif k > n:
            ans = messagebox.askyesnocancel(
                "字数与音符数不一致",
                f"琴谱有 {n} 个音，歌词有 {k} 个字，少了 {k - n} 个音。\n\n"
                "选「是」：把较长的音拆成两段补足（保证一字一音；拆不动的短音不动）\n"
                f"选「否」：多出来的字不配（只配前 {n} 个字）\n"
                "选「取消」：不处理",
            )
            if ans is None:
                return
            policy = "split" if ans else "ignore_chars"

        try:
            from piano_tool.midi_mapper import write_events

            min_hold = instruments.active().min_hold_ms or 30.0
            out, st = align_one_char_one_note(
                events, chars, policy=policy,
                use_tone=bool(res["use_tone"]), min_hold_ms=min_hold,
            )
            write_events(out, ev_path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("vocal match failed")
            messagebox.showerror("错误", f"人声匹配失败：{exc}")
            return

        tone_txt = ("参与（二声取最高 / 四声取最低）" if st["tone_used"] else "不参与")
        self.log(
            f"✓ 人声匹配完成：{st['notes_in']} 音 → {st['notes_out']} 个音，"
            f"配了 {min(len(chars), st['notes_out'])} 个字（一字一音）"
        )
        self.log(
            f"   合并多余音 {st['merged']} · 拆分长音 {st['split']} · "
            f"丢弃 {st['dropped']} · 未配上 {st['ignored_chars']} · 声调：{tone_txt}"
        )
        if st["tone_used"] and st["tone_conflicts"]:
            self.log(f"   ℹ 声调与旋律走向不一致（倒字）{st['tone_conflicts']} 处，"
                     "只作提示、没有改音高")
        self._lbl_map_stats.configure(
            text=(f"人声匹配：一字一音，共 {st['notes_out']} 个字 / 音 · "
                  f"合并 {st['merged']} · 拆分 {st['split']} · "
                  f"未配上 {st['ignored_chars']} · 声调{tone_txt}"),
            foreground="#1a7a1a",
        )
        if self._editor_server is not None:
            self.log("（浏览器编辑器若已打开，刷新页面即可看到匹配后的琴谱）")

    def _ask_vocal_match(self, note_count: int, events) -> "Optional[dict]":
        """人声匹配弹窗：自动识别音节 / 手动输入歌词。

        返回 {"mode": "auto", "audio": 路径} 或
        {"mode": "manual", "lyrics": str, "use_tone": bool}；取消返回 None。
        """
        from piano_tool.lyrics import parse_lyrics, tone_available
        from piano_tool.vocal_detect import find_vocal_tracks

        dlg = tk.Toplevel(self)
        dlg.title("人声匹配 · 一个音节一个音")
        dlg.transient(self)
        dlg.resizable(True, True)
        dlg.grab_set()

        nb = ttk.Notebook(dlg)
        nb.pack(fill="both", expand=True, padx=12, pady=(12, 0))

        # ---------- 自动识别：从音频里切音节，不用打字 ----------
        auto = ttk.Frame(nb, padding=10)
        nb.add(auto, text=" 自动识别（推荐） ")
        ttk.Label(
            auto,
            text=(f"当前琴谱有 {note_count} 个音。选歌曲音频（或①页分离出的人声轨），"
                  "自动检测人声音节，一个音节配一个音。\n"
                  "不用写歌词；也不识别文字（文字只影响挑音，不影响实际弹奏）。"),
            wraplength=580, justify="left",
        ).pack(anchor="w")

        var_audio = tk.StringVar()
        tracks = find_vocal_tracks(C.output_dir / "separated")
        if tracks:
            var_audio.set(str(tracks[0]))
        arow = ttk.Frame(auto)
        arow.pack(fill="x", pady=(10, 0))
        ttk.Label(arow, text="音频：").pack(side="left")
        ttk.Entry(arow, textvariable=var_audio).pack(side="left", fill="x", expand=True)
        ttk.Button(arow, text="浏览…",
                   command=lambda: self._pick_audio_into(var_audio)).pack(
                       side="left", padx=(6, 0))

        if tracks:
            hint = f"已找到①页分离出的人声轨：{tracks[0].name}（已自动填入，可直接用）"
            color = "#1a7a1a"
        else:
            hint = ("还没有人声轨。可以选歌曲原文件（带伴奏也能检测，但人声越干净越准）；"
                    "最准的做法是先在①页勾「先人声分离再转写」。")
            color = "#c0392b"
        ttk.Label(auto, text=hint, foreground=color, wraplength=580,
                  justify="left").pack(anchor="w", pady=(6, 0))
        ttk.Label(
            auto,
            text="检测依据：换气停顿 + 频谱通量字头；连唱的拖腔会在字头处切开。"
                 "音频与琴谱必须来自同一首歌，时间轴对不上时会有提示。",
            foreground="#666", wraplength=580, justify="left",
        ).pack(anchor="w", pady=(10, 0))

        # ---------- 手动输入歌词：原有流程 ----------
        man = ttk.Frame(nb, padding=10)
        nb.add(man, text=" 手动输入歌词 ")
        ttk.Label(
            man,
            text=(f"当前琴谱有 {note_count} 个音。把歌词粘贴到下面 —— "
                  "每个汉字会按顺序配一个音。\n标点、空格不占音，只当断句。"),
            wraplength=580, justify="left",
        ).pack(anchor="w")

        body = ttk.Frame(man)
        body.pack(fill="both", expand=True, pady=(6, 0))
        txt = tk.Text(body, width=62, height=9, wrap="char", font=("Microsoft YaHei", ui_scale.scaled(11)))
        scroll = ttk.Scrollbar(body, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=scroll.set)
        txt.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # 已经匹配过的琴谱：把原来的字填回去，方便改几个字再匹配
        old = "".join(str(getattr(e, "lyric", "") or "") for e in events)
        if old:
            txt.insert("1.0", old)

        cnt = ttk.Label(man, text="", foreground="#666")
        cnt.pack(anchor="w", pady=(6, 0))

        def _refresh(_e=None):
            k = len(parse_lyrics(txt.get("1.0", "end")))
            diff = k - note_count
            tail = "（正好一字一音）" if diff == 0 else (
                f"（比音符多 {diff} 个）" if diff > 0 else f"（比音符少 {-diff} 个）")
            cnt.configure(text=f"已识别 {k} 个字{tail}")

        txt.bind("<KeyRelease>", _refresh)
        _refresh()

        tone_var = tk.StringVar(value="off")
        tone_row = ttk.Frame(man)
        tone_row.pack(anchor="w", pady=(8, 0))
        ttk.Label(tone_row, text="声调：").pack(side="left")
        ttk.Radiobutton(tone_row, text="不参与（按首音）", value="off",
                        variable=tone_var).pack(side="left")
        rb_on = ttk.Radiobutton(
            tone_row, text="参与（二声取最高 / 四声取最低）", value="on",
            variable=tone_var)
        rb_on.pack(side="left", padx=(12, 0))
        if not tone_available():
            rb_on.configure(state="disabled")
            ttk.Label(tone_row, text="（需 pypinyin）",
                      foreground="#c0392b").pack(side="left")
        ttk.Label(
            man,
            text="声调只用来在「同一个字的几个音里挑代表音高」和提示「倒字」，"
                 "不会凭空生成新音高。",
            foreground="#666", wraplength=580, justify="left",
        ).pack(anchor="w", pady=(2, 0))

        result: dict = {}

        def _ok():
            if nb.index(nb.select()) == 0:
                p = var_audio.get().strip()
                if not p or not Path(p).is_file():
                    messagebox.showwarning("提示", "请先选择歌曲音频文件", parent=dlg)
                    return
                result.update({"mode": "auto", "audio": p})
            else:
                lyrics = txt.get("1.0", "end").strip()
                if not parse_lyrics(lyrics):
                    messagebox.showwarning("提示", "还没有输入歌词（标点不算字）", parent=dlg)
                    return
                result.update({"mode": "manual", "lyrics": lyrics,
                               "use_tone": tone_var.get() == "on"})
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.pack(fill="x", padx=12, pady=(10, 12))
        ttk.Button(btns, text="开始匹配", command=_ok).pack(side="right")
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right", padx=(0, 8))

        dlg.update_idletasks()
        # 居中到主窗口
        try:
            x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
            y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3
            dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:  # noqa: BLE001
            pass
        self.wait_window(dlg)
        return result or None

    def _pick_audio_into(self, var: tk.StringVar):
        """把人声匹配用的音频路径写进指定变量。"""
        p = filedialog.askopenfilename(
            title="选择歌曲音频（或①页分离出的 vocals.wav）",
            filetypes=[("音频文件", "*.wav *.mp3 *.flac *.m4a *.ogg *.aac"),
                       ("所有文件", "*.*")],
        )
        if p:
            var.set(p)

    def _vocal_match_auto(self, events, audio_path: str):
        """自动识别模式：从音频检测音节，一个音节一个音。"""
        from piano_tool.vocal_detect import align_notes_to_syllables, detect_syllables

        self.log(f"正在识别人声音节：{audio_path}")
        self.update_idletasks()
        try:
            syllables = detect_syllables(audio_path)
        except Exception as exc:  # noqa: BLE001
            logger.exception("syllable detection failed")
            messagebox.showerror("错误", f"识别失败：{exc}")
            return
        if not syllables:
            messagebox.showwarning(
                "提示",
                "没有检测到人声音节。\n请确认选的是这首歌的音频，"
                "或先在①页做「人声分离」再试。",
            )
            return

        min_hold = instruments.active().min_hold_ms or 30.0
        out, st = align_notes_to_syllables(
            events, syllables, tolerance_ms=90.0, min_hold_ms=min_hold
        )

        n = len(events)
        cov = float(st["coverage"])
        warn = ""
        if cov < 0.35:
            warn = (f"\n\n⚠ 只有 {cov * 100:.0f}% 的音落在人声音节内，"
                    "音频和这份琴谱很可能不是同一首歌（或时间轴对不上）。")
        msg = (
            f"检测到 {st['syllables']} 个音节。\n"
            f"琴谱 {n} 个音，其中 {st['inside']} 个落在音节内；\n"
            f"对齐后保留 {st['notes_out']} 个音（一个音节一个音），"
            f"丢弃非人声音符 {st['dropped']} 个，拆分跨字长音 {st['split']} 处。\n"
            f"{st['silent_syllables']} 个音节没有对应的音（不会凭空补音）。{warn}\n\n"
            "是否写回琴谱？"
        )
        if not messagebox.askokcancel("自动识别结果", msg, parent=self):
            self.log("已取消自动识别，琴谱未改动。")
            return

        try:
            from piano_tool.midi_mapper import write_events

            write_events(out, C.output_dir / "events.json")
        except Exception as exc:  # noqa: BLE001
            logger.exception("write auto vocal events failed")
            messagebox.showerror("错误", f"写入琴谱失败：{exc}")
            return

        self.log(
            f"✓ 自动识别人声完成：{st['syllables']} 个音节 → {st['notes_out']} 个音"
            f"（丢弃非人声音符 {st['dropped']} 个，拆分 {st['split']} 处跨字长音）"
        )
        self.log(
            f"   归属率 {cov * 100:.0f}% · 实测音高 {st['with_pitch']} 个音节 · "
            f"无声音节 {st['silent_syllables']}"
        )
        self._lbl_map_stats.configure(
            text=(f"自动识别人声：{st['syllables']} 个音节 / {st['notes_out']} 个音 · "
                  f"丢弃 {st['dropped']} · 拆分 {st['split']} · "
                  f"归属率 {cov * 100:.0f}%"),
            foreground="#1a7a1a",
        )
        if self._editor_server is not None:
            self.log("（浏览器编辑器若已打开，刷新页面即可看到对齐后的琴谱）")

    def _on_clean_events(self):
        """清理 events.json 里的米粒短音（背景音误识），保持旋律干净。"""
        ev_path = C.output_dir / "events.json"
        if not ev_path.exists():
            messagebox.showwarning("提示", "尚未生成 events.json，请先完成「② MIDI→琴谱」")
            return
        try:
            from piano_tool.midi_mapper import clean_events_keep_melody, write_events
            from piano_tool.player import load_events

            events = load_events(ev_path)
            min_dur = float(self._var_mindur.get())
            cleaned, stats = clean_events_keep_melody(
                events,
                min_duration=min_dur,
                dedupe_gap=0.06,
                melody=bool(self._var_mono.get()),
                melody_mode=self._var_melody_mode.get() or "high",
            )
            write_events(cleaned, ev_path)
            tail = (f" · 旋律抽取 {stats['melody_dropped']}") if stats["melody"] else ""
            self.log(
                f"✓ 已清理音符保持旋律：{stats['in']} → {stats['out']} 音符"
                f"（去掉米粒短音 {stats['short_dropped']}，重复音 {stats['dup_dropped']}{tail}）"
            )
            self._lbl_map_stats.configure(
                text=(f"清理后：{stats['out']} 音符 · 米粒短音 {stats['short_dropped']} · "
                      f"重复音 {stats['dup_dropped']}{tail} · 阈值 {min_dur*1000:.0f}ms"),
                foreground="#1a7a1a",
            )
            if self._editor_server is not None:
                self.log("（浏览器编辑器若已打开，刷新页面即可看到清理后的琴谱）")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("错误", f"清理失败：{exc}")

    def _on_soften(self):
        """高音柔化：过高/跳进过硬的音做八度平移（音级与调性不变）。"""
        ev_path = C.output_dir / "events.json"
        if not ev_path.exists():
            messagebox.showwarning("提示", "尚未生成 events.json，请先完成「② MIDI→琴谱」")
            return
        try:
            from piano_tool.midi_mapper import soften_melody_range, write_events
            from piano_tool.player import load_events

            ctxt = self._var_ceiling.get()
            ceiling = None
            if ctxt != "不限" and "(" in ctxt:
                ceiling = int(ctxt[ctxt.rfind("(") + 1:ctxt.rfind(")")])
            ltxt = self._var_maxleap.get()
            max_leap = None if ltxt == "不限" else int(ltxt)

            events = load_events(ev_path)
            softened, stats = soften_melody_range(
                events, ceiling=ceiling, max_leap=max_leap
            )
            write_events(softened, ev_path)
            self.log(
                f"✓ 已高音柔化：限幅降八度 {stats['ceiling_moved']} 音 · "
                f"跳进平滑 {stats['leap_smoothed']} 音"
                f"（只换八度，音级/调性不变）"
            )
            self._lbl_map_stats.configure(
                text=(f"柔化后：{stats['out']} 音符 · 限幅 {stats['ceiling_moved']} · "
                      f"跳进平滑 {stats['leap_smoothed']}"),
                foreground="#1a7a1a",
            )
            if self._editor_server is not None:
                self.log("（浏览器编辑器若已打开，刷新页面即可看到柔化后的琴谱）")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("错误", f"柔化失败：{exc}")

    def _on_organize(self):
        """音符整理：起点/时值对齐拍网格，节奏更稳（不改音高）。"""
        ev_path = C.output_dir / "events.json"
        if not ev_path.exists():
            messagebox.showwarning("提示", "尚未生成 events.json，请先完成「② MIDI→琴谱」")
            return
        try:
            from piano_tool.midi_mapper import organize_notes, write_events
            from piano_tool.player import load_events

            grid_div = {"1/4(四分)": 1, "1/8(八分)": 2, "1/16(十六分)": 4}.get(
                self._var_grid.get(), 2)
            bpm = float(self._var_bpm.get())
            events = load_events(ev_path)
            out, stats = organize_notes(
                events, bpm=(bpm if bpm > 0 else None), grid_div=grid_div
            )
            write_events(out, ev_path)
            src = "自动估计" if stats["bpm_estimated"] else "手动指定"
            self.log(
                f"✓ 已音符整理：{stats['in']} → {stats['out']} 音符 · "
                f"BPM {stats['bpm']:.0f}（{src}）· 网格 {stats['grid']*1000:.0f}ms · "
                f"移动 {stats['moved']} 个音（平均 {stats['moved_avg_ms']:.0f}ms）· "
                f"合并碎片 {stats['merged']}"
            )
            art = stats.get("articulation")
            if art:
                self.log(
                    f"  人声唱法已按新节奏重算：最短按住 {art['min_hold']*1000:.0f}ms · "
                    f"补足短音 {art['stretched']} · 连唱 {art['legato']} · "
                    f"让位重叠 {art['trimmed']} · 同音并成长音 {art.get('merged_same', 0)} · "
                    f"保留换气 {art['breathe']}"
                    "（时值不按网格取整，否则会把连唱和换气抹平）")
            if stats.get("same_tick") and stats["moved"]:
                self.log(f"ℹ 其中 {stats['same_tick']} 个音和别的音落在同一时刻"
                         f"（可能是量化拉拢，也可能原曲就是和弦）")
            if stats["moved"] == 0 and stats["merged"] == 0:
                self.log(
                    "ℹ 本次没有移动任何音符 —— 琴谱已经在当前网格上了（整理是幂等的，"
                    "再点多少遍都一样）。想看到变化：换更粗的网格（1/4）或把 BPM 改得"
                    "离自动估计值远一点。注意：BPM 只是网格标尺，**不是倍速** —— "
                    "想加速/减速请到 ⑤ 播放页调「倍速」。"
                )
            self._lbl_map_stats.configure(
                text=(f"整理后：{stats['out']} 音符 · BPM {stats['bpm']:.0f} · "
                      f"网格 {stats['grid']*1000:.0f}ms · 合并碎片 {stats['merged']} · "
                      f"移动 {stats['moved']} 音"
                      f"（平均 {stats['moved_avg_ms']:.0f}ms）"),
                foreground="#1a7a1a",
            )
            if self._editor_server is not None:
                self.log("（浏览器编辑器若已打开，刷新页面即可看到整理后的琴谱）")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("错误", f"整理失败：{exc}")

    def _on_open_editor(self):
        ev_path = C.output_dir / "events.json"
        if not ev_path.exists():
            messagebox.showwarning("提示", "尚未生成 events.json，请先完成「② MIDI→琴谱」")
            return
        try:
            port = self._start_editor_server()
            url = f"http://127.0.0.1:{port}/editor?events=/api/events"
            webbrowser.open(url)
            self.log(f"已在浏览器打开编辑器（可微调后点「💾 保存到项目」直接生效）：{url}")
            self._warn_if_proxy_blocks_localhost()
        except Exception as exc:  # noqa: BLE001
            logger.warning("编辑器本地服务启动失败，回退 file:// 模式：%s", exc)
            # 兜底：file:// 注入副本（此模式下没有自动保存回写）
            try:
                html = Path(C.editor_path).read_text(encoding="utf-8")
                data = ev_path.read_text(encoding="utf-8")
                out = C.output_dir / "editor_loaded.html"
                out.write_text(inject_events_into_editor(html, data), encoding="utf-8")
                webbrowser.open(out.resolve().as_uri())
                self.log(f"已在浏览器打开编辑器（file:// 模式，无自动保存）：{out}")
            except Exception as exc2:  # noqa: BLE001
                messagebox.showerror("错误", f"打开编辑器失败：{exc2}")

    def _warn_if_proxy_blocks_localhost(self):
        """系统代理若未排除本机地址，浏览器可能打不开本地编辑器页面（404/空白）。

        真实场景：用户开着代理软件（Clash/v2ray 等）且没把 localhost 加入直连名单，
        浏览器请求 127.0.0.1 时会被发给代理，代理返回 404 —— 用户只看到空白页，
        不知道是代理在捣乱。这里提前给出可操作的提示。
        """
        try:
            proxies = urllib.request.getproxies()
        except Exception:  # noqa: BLE001
            return
        http_proxy = proxies.get("http") or proxies.get("https")
        if not http_proxy:
            return
        no_proxy = str(proxies.get("no") or "")
        if "127.0.0.1" in no_proxy or "localhost" in no_proxy:
            return
        self.log(f"⚠ 检测到系统代理 {http_proxy} 且未排除本机地址：若编辑器页面"
                 f"空白或 404，请在代理软件里把 127.0.0.1 / localhost 加入"
                 f"「不使用代理」名单，再刷新页面。")

    def _start_editor_server(self) -> int:
        """启动（或复用）编辑器本地服务，返回端口。线程安全、幂等。"""
        with self._editor_lock:
            if self._editor_server is not None:
                return int(self._editor_port)  # type: ignore
            server = ThreadingHTTPServer(("127.0.0.1", 0), _EditorHandler)
            port = server.server_address[1]
            _EditorHandler.app_ref = self        # /api/ping 要读播放状态
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            self._editor_server = server
            self._editor_port = port
            return port

    def _stop_editor_server(self):
        with self._editor_lock:
            srv = self._editor_server
            self._editor_server = None
            self._editor_port = None
        if srv is not None:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:  # noqa: BLE001
                pass

    # ----------------------------------------------------------- ③ 校准
    def _on_screenshot(self):
        try:
            import cv2
            import mss
            from mss import tools as mss_tools

            from piano_tool.imio import imread_unicode

            C.data_dir.mkdir(parents=True, exist_ok=True)
            full = str(C.data_dir / "screenshot.png")
            screen = self._selected_screen()
            with mss.mss() as sct:
                if screen is not None:
                    # 只截选中的那块屏：坐标从这块屏的左上角算起，
                    # 播放时再加上它的原点即可（双屏任意摆法都对）
                    region = {"left": screen.left, "top": screen.top,
                              "width": screen.width, "height": screen.height}
                    shot = sct.grab(region)
                    mss_tools.to_png(shot.rgb, shot.size, output=full)
                    self._shot_screen = screen
                else:
                    sct.shot(output=full)          # 拿不到显示器信息时退回截图拼接
                    self._shot_screen = None
            img = imread_unicode(full)
            if img is None:
                raise RuntimeError("截屏失败，无法读取图像")
            self._shot_bgr = img
            h, w = img.shape[:2]

            # 缩放到画布可容纳的大小（绝对尺寸上限由 safe_photo_from_bgr
            # 兜底，防止 Windows 上 Tk 位图分配失败）
            canvas_w = max(self._canvas.winfo_width() - 4, 400)
            canvas_h = max(self._canvas.winfo_height() - 4, 300)
            scale = min(canvas_w / w, canvas_h / h, 1.0)
            disp = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))),
                              interpolation=cv2.INTER_AREA)

            # 先释放上一张预览图，再创建新图：旧图仍被 canvas 引用时
            # 两张图同时占内存，正是 CreateDIBSection 失败的诱因之一
            self._canvas.delete("all")
            self._photo = None
            photo, pw, ph = safe_photo_from_bgr(disp)
            self._photo = photo
            self._canvas.create_image(0, 0, image=self._photo, anchor="nw")
            # 用实际显示的尺寸回填缩放比（自动缩小时保证角点/琴键对齐）
            self._shot_scale = pw / float(w)

            self._corners = place_default_corners(w, h)
            self._cal = None
            self._redraw_overlay()
            where = f"（{self._shot_screen.label}）" if self._shot_screen else "（全部显示器拼接）"
            self._lbl_cal.configure(
                text=f"已截屏{where}。请把 4 个红点拖到琴键区四角，然后点「计算 {len(instruments.active().keys)} 键并保存」。",
                foreground="#1a5fb4")
            self.log(f"截屏完成{where}：{w}×{h}，预览 {pw}×{ph}，显示缩放 {self._shot_scale:.3f}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("screenshot failed")
            messagebox.showerror("错误", f"截屏失败：{exc}")

    def _img2disp(self, x: float, y: float) -> Tuple[float, float]:
        return x * self._shot_scale, y * self._shot_scale

    def _disp2img(self, x: float, y: float) -> Tuple[float, float]:
        return x / self._shot_scale, y / self._shot_scale

    def _redraw_overlay(self):
        cv = self._canvas
        cv.delete("overlay")
        if self._photo is None or not self._corners:
            return
        pts = [self._img2disp(x, y) for x, y in self._corners]
        # 四角连线（闭合）
        for i in range(4):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % 4]
            cv.create_line(x1, y1, x2, y2, fill="#ffd166", width=2, dash=(6, 4), tags="overlay")
        for i, (x, y) in enumerate(pts):
            r = 8
            cv.create_oval(x - r, y - r, x + r, y + r, fill="#e5484d", outline="white",
                           width=2, tags=("overlay", f"handle{i}"))
            cv.create_text(x, y - 16, text=f"{i + 1} {CORNER_LABELS[i]}", fill="#ffffff",
                           font=("Microsoft YaHei", ui_scale.scaled(10), "bold"), tags="overlay")
        # 已有校准结果：把所有琴键画出来
        if self._cal is not None:
            for k in self._cal.keys:
                x, y = self._img2disp(k.x, k.y)
                cv.create_oval(x - 4, y - 4, x + 4, y + 4, fill="#2ec27e", outline="",
                               tags="overlay")
                cv.create_text(x + 7, y - 6, text=k.key, fill="#2ec27e",
                               font=("Consolas", ui_scale.scaled(9), "bold"), tags="overlay")

    def _on_canvas_press(self, event):
        if not self._corners or self._shot_scale <= 0:
            return
        ix, iy = self._disp2img(event.x, event.y)
        radius_img = 14 / self._shot_scale
        idx = find_nearest_corner(self._corners, ix, iy, radius_img)
        self._drag_idx = idx

    def _on_canvas_drag(self, event):
        if self._drag_idx is None or not self._corners:
            return
        ix, iy = self._disp2img(event.x, event.y)
        h, w = self._shot_bgr.shape[:2]
        self._corners[self._drag_idx] = [max(0.0, min(float(w), ix)),
                                         max(0.0, min(float(h), iy))]
        self._cal = None  # 角点变了，旧结果作废
        self._redraw_overlay()

    def _on_canvas_release(self, _event):
        self._drag_idx = None

    def _on_compute_calibration(self):
        if self._shot_bgr is None or len(self._corners) != 4:
            messagebox.showwarning("提示", "请先「截取屏幕」，并拖好 4 个角点")
            return
        try:
            import cv2  # noqa: F401

            from piano_tool.locator import build_calibration, draw_debug_image, save_calibration

            h, w = self._shot_bgr.shape[:2]
            # 记下这张截图来自哪块屏：播放时 key 坐标 + 这块屏的原点 = 屏幕物理坐标
            shot_screen = self._shot_screen
            cal = build_calibration(
                self._corners, image_size=[w, h],
                screen_origin=list(shot_screen.origin) if shot_screen else None,
                screen_index=shot_screen.index if shot_screen else None,
            )
            save_calibration(cal, C.calibration_path)
            # 这次存的是「屏幕坐标」校准，若旧文件还是键盘方式就同步一下勾选框（只读不改文件）
            self._sync_keyopt_from_file()
            draw_debug_image(self._shot_bgr, cal, C.debug_image)
            self._cal = cal
            self._redraw_overlay()
            screen_note = (f"，截图来自「{shot_screen.label}」，"
                           f"点击时会加上原点 {shot_screen.left},{shot_screen.top}"
                           if shot_screen else "，未记录显示器（单屏可用，双屏请先选演奏显示器）")
            self._lbl_cal.configure(
                text=f"✓ 校准完成：{len(cal.keys)} 键坐标已保存到 {C.calibration_path.name}"
                     f"（{instruments.active().name}{screen_note}）",
                foreground="#1a7a1a",
            )
            self.log(f"✓ 校准完成。可点「查看调试图」人工核对 {len(cal.keys)} 个绿点是否都落在键上。")
        except Exception as exc:  # noqa: BLE001
            logger.exception("calibrate failed")
            messagebox.showerror("错误", f"校准失败：{exc}")

    def _on_open_debug(self):
        if not Path(C.debug_image).exists():
            messagebox.showinfo("提示", "尚未生成调试图（校准后自动生成）")
            return
        try:
            os.startfile(str(C.debug_image))  # Windows 图片查看器
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("错误", f"打开调试图失败：{exc}")

    # ------------------------------------------------------------- ④ 播放
    def _require_calibration(self):
        """返回 (校准, 弹奏方式)。

        弹奏方式只看③ 页那个「原神键位优化」勾选框：不勾 = 老方案（鼠标），
        勾了 = 键盘弹奏。键盘方式直接返回坐标全 0 的占位校准，跳过屏幕越界检查。
        """
        from piano_tool.keyboard_play import (
            INPUT_MODE_KEYBOARD,
            INPUT_MODE_SCREEN,
            resolve_calibration,
        )

        mode = INPUT_MODE_KEYBOARD if bool(self._var_keyopt.get()) else INPUT_MODE_SCREEN
        try:
            cal = resolve_calibration(mode, C.calibration_path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("错误", f"读取校准失败：{exc}")
            return None, mode
        if mode == INPUT_MODE_KEYBOARD:
            return cal, mode
        if not Path(C.calibration_path).exists():
            messagebox.showwarning("提示", "尚未校准，请先完成「③ 四点校准」")
            return None, mode
        # 带上当前演奏屏尺寸：换过显示器/改过系统缩放时，老校准会整体错位，
        # 这种情况「坐标是否越界」查不出来，必须按尺寸不符拦下重做
        screen = self._selected_screen()
        ok, msg = check_coords_on_screen(
            cal, expect_size=screen.size if screen else None)
        self.log(msg)
        if not ok:
            messagebox.showwarning("坐标可能不正确", msg)
            return None, mode
        return cal, mode

    def _on_play(self):
        events_path = C.output_dir / "events.json"
        if not events_path.exists():
            messagebox.showwarning("提示", "尚未生成 events.json，请先完成「② MIDI→琴谱」")
            return
        cal, mode = self._require_calibration()
        if cal is None:
            return
        if self._busy:
            return
        dry = bool(self._var_dry.get())

        # 伴奏：放 ① 人声分离出的 no_vocals.wav（纯伴奏）。这里在主线程把路径
        # 算好再交给播放线程 —— Tk 变量不能在工作线程里读。
        backing_path, backing_vol = "", 0.6
        if bool(self._var_backing.get()):
            try:
                backing_vol = max(0, min(150, int(self._var_backing_vol.get()))) / 100.0
            except (TypeError, ValueError):
                backing_vol = 0.6
            from piano_tool import backing_track

            src = backing_track.normalize_source(self._var_backing_src.get())
            wav, why = backing_track.find_backing(self._var_audio.get().strip() or None, src)
            self.log(("♪ " if wav else "⚠ 伴奏：") + why)
            if wav:
                backing_path = str(wav)

        self._play_stop.clear()
        self._play_paused = False
        self._resume_position = 0.0
        self._playing = True
        self._set_busy(True, self._btn_play, "播放中…（F7 停止 / 三连右键暂停）")
        self._start_gesture_watch()
        self._btn_stop.configure(state="normal")
        if not dry:
            # 真实点击：先最小化本窗口，避免窗口挡住模拟器导致点在自己身上
            self.iconify()
            self.log("本窗口已最小化到任务栏，请在倒计时内把浏览器模拟器置于最前。")
        self._last_play_args = (
            events_path, dry, float(self._var_speed.get()),
            int(self._var_delay.get()), int(self._var_countdown.get()),
            float(self._var_latency.get()),
            bool(self._var_hold.get()), float(self._var_holdmin.get()),
            mode, bool(self._var_focus_guard.get()),
            str(self._var_focus_lost.get() or "wait"),
            self._var_focus_word.get().strip(),
            backing_path, backing_vol,
        )
        threading.Thread(target=self._play_worker, args=self._last_play_args,
                         daemon=True).start()

    def _on_self_test(self):
        """只敲一下选中的键：屏幕校准看坐标对不对，键盘优化看字母键认不认。"""
        from piano_tool.keyboard_play import INPUT_MODE_KEYBOARD

        cal, mode = self._require_calibration()
        if cal is None:
            return
        key = self._var_testkey.get().strip()
        if not key:
            messagebox.showwarning("提示", "请先选择要自检的键")
            return
        if mode == INPUT_MODE_KEYBOARD:
            if self._busy:
                return
            self._play_stop.clear()
            self._playing = True
            self._set_busy(True, self._btn_test, "自检中…（F7 停止）")
            self.iconify()
            upper = "（发送大写）" if self._var_upper.get() else ""
            self.log(f"键盘自检：{self._var_countdown.get()} 秒后敲一下字母键 {key}{upper}")
            threading.Thread(
                target=self._selftest_key_worker,
                args=(key, int(self._var_countdown.get()), bool(self._var_upper.get()),
                      bool(self._var_focus_guard.get()), self._var_focus_word.get().strip()),
                daemon=True,
            ).start()
            return
        coord = {k.key: (k.x, k.y) for k in cal.keys}.get(key)
        if coord is None:
            messagebox.showerror("错误", f"校准里没有键 {key}")
            return
        if self._busy:
            return
        self._play_stop.clear()
        self._playing = True
        self._set_busy(True, self._btn_test, "自检中…（F7 停止）")
        self.iconify()
        self.log(f"点击自检：{self._var_countdown.get()} 秒后点击键 {key} @ ({coord[0]:.0f}, {coord[1]:.0f})")
        threading.Thread(target=self._selftest_worker, args=(key, coord, int(self._var_countdown.get())),
                         daemon=True).start()

    def _selftest_worker(self, key: str, coord: Tuple[float, float], countdown: int):
        try:
            import pyautogui

            counts = max(countdown, 3)
            for i in range(counts, 0, -1):
                if self._play_stop.is_set():
                    self._q.put(("selftest_done", "自检已取消"))
                    return
                self._q.put(("play_status", f"自检倒计时 {i} 秒…（请把模拟器置于最前）"))
                time.sleep(1.0)
            pyautogui.click(coord[0], coord[1])
            self._q.put(("log", f"✓ 已真实点击键 {key} @ ({coord[0]:.0f}, {coord[1]:.0f})"))
            self._q.put(("selftest_done", "自检完成：看模拟器上该键是否响了"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("self test failed")
            self._q.put(("log", f"✗ 自检失败：{exc}"))
            self._q.put(("selftest_done", f"错误：{exc}"))

    def _selftest_key_worker(self, key: str, countdown: int, upper: bool = False,
                             focus_guard: bool = False, focus_word: str = ""):
        """键盘映射方式下的自检：倒计时后敲一下对应的字母键。"""
        try:
            from piano_tool.keyboard_play import tap_key

            focus_check = None
            if focus_guard:
                from piano_tool.focus_guard import make_checker

                focus_check = make_checker(getattr(self, "_focus_target", None), focus_word)
            counts = max(countdown, 3)
            for i in range(counts, 0, -1):
                if self._play_stop.is_set():
                    self._q.put(("selftest_done", "自检已取消"))
                    return
                self._q.put(("play_status", f"自检倒计时 {i} 秒…（请把模拟器置于最前）"))
                time.sleep(1.0)
            if focus_check is not None:
                ok, fg_title = focus_check()
                if not ok:
                    self._q.put((
                        "log",
                        f"✗ 当前前台不是目标窗口（{fg_title or '未知窗口'}），已放弃自检 —— "
                        f"再敲也只是打给这个窗口，目标窗口收不到",
                    ))
                    self._q.put(("selftest_done", "自检取消：焦点不在目标窗口"))
                    return
            tap_key(key, upper=upper)
            self._q.put(("log", f"✓ 已敲一下字母键 {key}（{'大写' if upper else '小写'}），看模拟器上该键是否响了"))
            self._q.put(("selftest_done", "自检完成：看模拟器上该键是否响了"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("keyboard self test failed")
            self._q.put(("log", f"✗ 自检失败：{exc}"))
            self._q.put(("selftest_done", f"错误：{exc}"))

    def _on_q_selftest_done(self, msg):
        self._playing = False
        self._set_busy(False, self._btn_test, "🎯 点击自检（只点一下）")
        self.deiconify()
        self._lbl_play.configure(text=msg, foreground="#1a7a1a")

    def _play_worker(self, events_path: Path, dry_run: bool, speed: float,
                     delay_ms: int, countdown: int, latency_ms: float = 0.0,
                     hold: bool = False, hold_min_ms: float = 30.0,
                     input_mode: str = "screen",
                     focus_guard: bool = False, focus_lost: str = "wait",
                     focus_word: str = "",
                     backing_path: str = "", backing_vol: float = 0.6,
                     start_at: float = 0.0):
        bg = None          # 伴奏实例（finally 里一定要停）
        try:
            from piano_tool.player import load_calibration, load_events, play

            # ---- 谱子自带的乐器：编辑器保存时会写 events_instrument.json。
            # 不同乐器的键位字母不同（C5 在风物之诗琴是 A、在首席女高音是 Q），
            # 谱子与当前乐器不一致时字母会整体错位 —— 这里明确提醒，
            # 但不静默改全局乐器（② 页的选择才是准的）。
            try:
                meta_path = Path(events_path).parent / "events_instrument.json"
                if meta_path.exists():
                    spec = json.loads(meta_path.read_text(encoding="utf-8")).get("instrument")
                    if spec:
                        from piano_tool import instruments
                        cur = instruments.active()
                        if str(getattr(cur, "id", "")) != str(spec):
                            inst = instruments.get_instrument(spec)
                            self._q.put(("log",
                                        f"📜 这份谱子的键位是按「{inst.name}」生成的，"
                                        f"当前乐器是「{cur.name}」—— 字母会对不上，"
                                        f"请到②页把乐器切成「{inst.name}」再弹"))
            except Exception as exc:  # noqa: BLE001  读不到就按当前乐器走
                logger.warning("read events_instrument.json failed: %s", exc)

            # ---- 焦点守卫：只在键盘方式下有意义（注入按键只对前台窗口生效）----
            focus_check = None
            if focus_guard and str(input_mode) == "keyboard":
                from piano_tool.focus_guard import make_checker

                # focus_word 在主线程读好后传进来：tkinter 变量不能跨线程读
                focus_check = make_checker(getattr(self, "_focus_target", None), focus_word)
                if focus_check is None:
                    self._q.put(("log", "⚠ 焦点守卫已勾选但没指定目标窗口，本次仍不做焦点检查"))
                else:
                    ok, fg_title = focus_check()
                    self._q.put((
                        "log",
                        f"焦点守卫：{'✓ 目标窗口在前台，开始弹奏' if ok else '⚠ 当前前台不是目标窗口（' + (fg_title or '未知窗口') + '）'}"
                        + ("" if ok else f"，已切到「{ {'wait': '暂停等待', 'skip': '静音跳过', 'stop': '直接停止'}.get(focus_lost, '暂停等待') }」模式"),
                    ))

            def _status(msg: str) -> None:
                self._q.put(("play_status", msg))

            if countdown > 0:
                for i in range(countdown, 0, -1):
                    if self._play_stop.is_set():
                        self._q.put(("play_done", "已取消"))
                        return
                    self._q.put(("play_status", f"倒计时 {i} 秒后开始…"))
                    time.sleep(1.0)

            events = load_events(events_path)
            # 鼠标模式按住时值的硬限制：一个鼠标同一时刻只能按住一个键。
            # 长音还没结束、下一个音就起音时，只能先松开长音；这不是代码没实现，
            # 是单指针的物理限制。键盘模式才能同时按住多个键。
            if str(input_mode) == "screen" and hold and events:
                _evs = sorted(events, key=lambda e: float(e.time))
                _overlap = 0
                for _a, _b in zip(_evs, _evs[1:]):
                    _dur = float(_a.duration or 0.0)
                    if _dur > 0 and float(_a.time) + _dur > float(_b.time) + 1e-6:
                        _overlap += 1
                if _overlap:
                    self._q.put(("log",
                                 f"⚠ 鼠标模式同一时刻只能按住一个键：检测到 {_overlap} 个长音"
                                 f"会被下一个音提前松开。想真正按住长音/和弦，请到③页勾选"
                                 f"「原神键位优化」改用键盘弹奏。另：风物之诗琴是拨弦音色，"
                                 f"按住不会像编辑器合成器那样持续发声，长音靠自然余音。"))
            # 键盘方式不用屏幕坐标，calibration.json 可能压根不存在（全新安装 / 清过 data）。
            # 统一走 calibration_for_input_mode，它按方式分支、不无条件读文件。
            cal = calibration_for_input_mode(input_mode)
            coord0 = {k.key: (k.x, k.y) for k in cal.keys}
            first = events[0] if events else None
            if str(input_mode) == "keyboard":
                mode = "键盘弹奏（Q~U / A~J / Z~M）" + (" · 按住时值" if hold else "")
            elif dry_run:
                mode = "试运行（不真实点击）"
            else:
                mode = "真实点击 · 按住时值" if hold else "真实点击"
            # ④「逐键采样」的真正用途是采集每个键的琴声，给浏览器编辑器的
            # 「模拟音」用。采样时顺带测到的延迟**不再参与播放** —— 逐键提前量
            # 会改变音与音的相对时间，同一份谱子会错拍/不和谐（用户实测旧版
            # 没有这份样本、用统一提前量反而更和谐）。播放只用统一提前量。
            self._latency_lead = {}
            lead = None
            if Path(C.latency_profile_path).exists():
                self._q.put(("log",
                             "④ 逐键音色库存在（供编辑器模拟音用）；"
                             "播放使用统一提前量，不再逐键补偿。"))
            self._q.put(("log", f"开始播放：{len(events)} 个音符，模式：{mode}"
                                + (f"（从 {start_at:.1f}s 继续）" if start_at else "")))
            if first is not None:
                if str(input_mode) == "keyboard":
                    self._q.put(("log",
                                 f"第一个音 t={first.time:.3f}s 键={first.key}（键盘方式，不用坐标）"))
                else:
                    c = coord0.get(first.key, (0, 0))
                    self._q.put(("log",
                                 f"第一个音 t={first.time:.3f}s 键={first.key} 坐标=({c[0]:.0f}, {c[1]:.0f})"))
            if not dry_run:
                self._q.put(("log", f"按住时值：{'开启' if hold else '关闭'}（最小按住 {hold_min_ms:.0f}ms）"))
            self._q.put(("play_status",
                         f"播放中…（{len(events)} 个音符，{mode}）"))
            # 播放时钟起跳：编辑器据此把播放头跟着走（倒计时期间不算播放）
            try:
                total_s = max((float(e.time) + float(e.duration or 0.0)) for e in events) if events else 0.0
            except Exception:  # noqa: BLE001
                total_s = 0.0
            self._start_play_clock(speed=speed, total=total_s, start_time=start_at)

            # 伴奏：与主播放同一时刻起、从同一个位置开始（暂停继续时 start_at
            # 是暂停点，所以伴奏也能接上）。放不出来只记一条日志，演奏照常。
            if backing_path:
                from piano_tool.backing_track import BackingTrack

                bg = BackingTrack(backing_path, volume=backing_vol, speed=speed)
                if bg.start(from_sec=start_at):
                    self._backing = bg
                    bp = Path(backing_path)
                    # 纯伴奏文件名都是 no_vocals.wav，得带上歌名才看得出放的是哪首；
                    # 原曲直接就是文件名。
                    label = f"{bp.parent.name}/{bp.name}" if bp.name == "no_vocals.wav" else bp.name
                    self._q.put(("log",
                                 f"♪ 伴奏已同步播放：{label}（音量 {int(backing_vol * 100)}%"
                                 + (f"，{speed:g}× 变速会变调" if abs(speed - 1.0) > 1e-6 else "")
                                 + "）"))
                    if bg.warning:
                        self._q.put(("log", f"⚠ 伴奏：{bg.warning}"))
                else:
                    self._q.put(("log", f"⚠ 伴奏没放：{bg.warning}"))
                    bg = None

            devs = play(
                events, cal, speed=speed, delay_ms=delay_ms, dry_run=dry_run,
                start_time=start_at,
                stop_check=self._play_stop.is_set,
                latency_comp_ms=latency_ms,
                latency_lead=lead,
                hold=hold, hold_min_ms=hold_min_ms,
                input_mode=input_mode,
                focus_check=focus_check,
                on_focus_lost=focus_lost,
                status=_status if focus_check is not None else None,
            )
            if devs:
                import statistics

                avg = statistics.mean(devs) * 1000
                # 最大偏差 / 抖动：平均偏差看不出「一阵一阵」，峰值和标准差才能。
                # 最大偏差明显大于平均值 = 个别音符被拖后（听感不匀）；两者都大 = 整体滞后。
                self._q.put(("log", f"✓ 播放结束：{len(devs)} 次{'模拟点击' if not dry_run else '计划音符'}，"
                                    f"平均时间偏差 {avg:.1f} ms，"
                                    f"最大 {max(devs) * 1000:.1f} ms，"
                                    f"抖动(标准差) {statistics.pstdev(devs) * 1000:.1f} ms"))
            else:
                self._q.put(("log", "播放结束（未产生点击）"))
            self._q.put(("play_done", "播放完成" if not self._play_stop.is_set() else "已停止"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("play failed")
            self._q.put(("log", f"✗ 播放失败：{exc}"))
            self._q.put(("play_done", f"错误：{exc}"))
        finally:
            # 无论正常放完、被停止还是异常，都必须收回音频设备
            if bg is not None:
                bg.stop()
            self._backing = None

    def _on_stop(self):
        """真正停止（清掉暂停点）：按钮/F7 走这里。"""
        was_paused = self._play_paused
        self._play_paused = False
        self._resume_position = 0.0
        self._play_stop.set()
        if self._backing is not None:
            self._backing.stop()           # 马上静音（worker 收尾时还会再 stop 一次，幂等）
            self._backing = None
        self._freeze_play_clock()          # 立刻冻结播放头位置（编辑器马上停住）
        self._stop_gesture_watch()
        if was_paused and not self._playing:
            # 暂停时播放线程已经退出，不会再收到 play_done → 这里直接复位界面
            self._set_busy(False, self._btn_play, "▶ 开始播放")
            self._btn_stop.configure(state="disabled")
            if hasattr(self, "_lbl_play"):
                self._lbl_play.configure(text="已停止", foreground="#666")
        self.log("已请求停止（若正在真实点击，也可把鼠标甩到左上角紧急停止）")

    def _pause_playback(self) -> bool:
        """暂停：记住当前位置后让播放线程收尾退出（恢复时从那一点接着弹）。"""
        if not self._playing or self._play_paused:
            return False
        self._play_paused = True
        self._play_stop.set()
        # 立刻掐掉伴奏：播放线程要等几十毫秒才收尾，歌还在响会显得「按了没反应」。
        # 继续时 worker 会带着暂停点重新 start，所以不用在这里记位置。
        if self._backing is not None:
            self._backing.stop()
        self._freeze_play_clock()
        self._resume_position = self.play_state_snapshot()["position"]
        # 监听不停：暂停后还要靠三连右键恢复
        self.log(f"⏸ 已暂停在 {self._resume_position:.1f}s —— 再快速三按右键继续，点「■ 停止」才结束")
        if hasattr(self, "_lbl_play"):
            self._lbl_play.configure(
                text=f"已暂停在 {self._resume_position:.1f}s（三连右键继续）",
                foreground="#b58900")
        return True

    def _resume_playback(self) -> bool:
        """从暂停点继续（沿用本次播放的全部参数，不再倒计时）。"""
        if not self._play_paused or not self._last_play_args:
            return False
        self._play_paused = False
        self._play_stop.clear()
        self._playing = True
        self._set_busy(True, self._btn_play, "播放中…（F7 停止 / 三连右键暂停）")
        self._btn_stop.configure(state="normal")
        start = float(self._resume_position)
        args = list(self._last_play_args)
        args[4] = 0                       # 恢复不再倒计时（第 5 个参数是 countdown）
        self.log(f"▶ 继续播放：从 {start:.1f}s 接着弹")
        threading.Thread(target=self._play_worker, args=tuple(args) + (start,),
                         daemon=True).start()
        return True

    # ---------------- 播放中的手势：快速三按鼠标右键 = 暂停 ----------------
    def _start_gesture_watch(self):
        """播放开始后监听三连右键。回调在监听线程里，所以只往队列丢消息。"""
        from piano_tool.gestures import TripleRightClickWatcher

        self._stop_gesture_watch()
        self._gesture = TripleRightClickWatcher(
            on_triple=lambda: self._q.put(("gesture_stop", None)))
        self._gesture.start()

    def _stop_gesture_watch(self):
        w = getattr(self, "_gesture", None)
        if w is not None:
            try:
                w.stop()
            except Exception:  # noqa: BLE001
                pass
            self._gesture = None

    def _on_q_gesture_stop(self, _payload=None):
        """三连右键：播放中 → 暂停；已暂停 → 继续（同一个手势来回切换）。"""
        if self._play_paused:
            self.log("▶ 检测到「快速三按鼠标右键」→ 继续播放。")
            self._resume_playback()
            return
        if not self._playing:
            return
        self.log("⏸ 检测到「快速三按鼠标右键」→ 暂停播放。")
        self._pause_playback()

    # ---------------- 播放时钟：给浏览器编辑器同步播放头 ----------------
    def _start_play_clock(self, speed: float, total: float, start_time: float = 0.0):
        """开播（倒计时结束后）打点，之后位置由「已过时间 × 倍速」推算。"""
        self._play_clock.start(speed=speed, total=total, start_time=start_time)

    def _freeze_play_clock(self):
        """停止/暂停时把当前位置冻结下来（编辑器就停在那个位置，不会跳回开头）。"""
        self._play_clock.freeze()

    def play_state_snapshot(self) -> dict:
        """当前播放状态（HTTP 端点与自检都用它）。"""
        return self._play_clock.snapshot()

    def _on_play_with_editor(self):
        """「播放 + 同时编辑」：先打开浏览器编辑器，再启动播放。

        编辑器顶部有连接状态灯（PASS 绿 / FAIL 红），靠 `/api/ping` 判断
        软件是否在跑；播放中边听边改谱，改完点「保存到项目」即回写 events.json。
        """
        if not (C.output_dir / "events.json").exists():
            messagebox.showwarning("提示", "尚未生成 events.json，请先完成「② MIDI→琴谱」")
            return
        if self._busy:
            return
        self._on_open_editor()
        self._on_play()

    def _on_q_play_status(self, msg):
        self._lbl_play.configure(text=msg + " · 按 F7 停止 / 快速三按右键暂停",
                                 foreground="#1a5fb4")

    def _on_q_play_done(self, msg):
        if self._play_paused:
            # 暂停导致的线程退出：保持「继续播放」状态，别复位成空闲
            self._playing = False
            self._freeze_play_clock()
            self._set_busy(True, self._btn_play, "▶ 继续播放（已暂停）")
            self._btn_stop.configure(state="normal")
            if hasattr(self, "_lbl_play"):
                self._lbl_play.configure(
                    text=f"已暂停在 {self._resume_position:.1f}s（三连右键继续）",
                    foreground="#b58900")
            return
        self._playing = False
        self._freeze_play_clock()           # 播放结束也把位置冻结住（编辑器停在末尾）
        self._stop_gesture_watch()          # 播放结束就别再监听右键手势了
        self._set_busy(False, self._btn_play, "▶ 开始播放")
        self._btn_stop.configure(state="disabled")
        if not self._var_dry.get():
            try:
                self.deiconify()  # 真实播放时最小化过，播放结束恢复窗口
            except Exception:  # noqa: BLE001
                pass
        color = "#1a7a1a" if msg == "播放完成" else "#666"
        self._lbl_play.configure(text=msg, foreground=color)

    # ------------------------------------------------------------- 杂项
    def _set_busy(self, busy: bool, btn: ttk.Button, busy_text: str):
        self._busy = busy
        if busy:
            btn.configure(state="disabled", text=busy_text)
        else:
            btn.configure(state="normal", text=busy_text)

    def _on_close(self):
        self._stop_gesture_watch()        # 别留监听线程
        self._save_session_preset()       # 记住 ②③④⑤ 的设置，下次启动沿用
        self._save_window_geometry()      # 记住窗口大小/位置/最大化状态
        self._play_stop.set()
        self._stop_editor_server()
        if self._hotkey is not None:
            try:
                self._hotkey.unhook_all_hotkeys()
            except Exception:  # noqa: BLE001
                pass
        self.destroy()


def main():
    # 先声明 DPI 感知再建窗口（WindsongApp.__init__ 里也会调一次，幂等）：
    # 顺序反了的话，播放时首次注入按键才切换，窗口会突然按物理像素重绘 → 字体变小。
    ui_scale.ensure_dpi_aware_early()
    app = WindsongApp()
    app.mainloop()


if __name__ == "__main__":
    main()
