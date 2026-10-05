"""风物之诗琴自动化工具 · 桌面版 GUI（Tkinter）

一个 Python 桌面软件，四步向导：
    ① 音频 → MIDI（basic_pitch 转写，后台线程）
    ② MIDI → 21 白键琴谱（events.json）
    ③ 四点校准：截取屏幕 → 在截图上拖动 4 个角点对准琴键区四角
       → 透视变换自动算出 21 个键中心坐标
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
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from piano_tool.config import LOWEST_MIDI, WHITE_KEYS, get_config
from piano_tool.midi_mapper import fmt_range

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


class _EditorHandler(BaseHTTPRequestHandler):
    """给「在浏览器微调」用的极简本地服务（仅 127.0.0.1）。

    让编辑器走 HTTP 而非 file://，从而：
      - 支持 ?events=/api/events 自动加载（fetch 不再被浏览器拦截）
      - 提供 POST /api/save-events，把微调结果直接写回 data/output/events.json
        闭环到播放（播放器每次都从磁盘重新读 events.json）。
    """

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
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path == "/api/save-events":
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"[]"
                data = json.loads(raw.decode("utf-8"))
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
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": f"写入失败：{exc}"}))
                return
            self._send(200, json.dumps({"ok": True, "count": len(data)}))
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


def check_coords_on_screen(calibration, screen_size: Optional[Tuple[int, int]] = None) -> Tuple[bool, str]:
    """校验校准坐标是否落在真实屏幕范围内（pyautogui 逻辑坐标）。

    高分屏 DPI 缩放不一致时，截图是物理像素而 pyautogui 用逻辑像素，
    会导致点击位置整体偏移甚至点不到；这里给出明确提示。

    screen_size 可显式传入（便于测试），默认取 pyautogui.size()。
    """
    if screen_size is None:
        import pyautogui

        screen_size = pyautogui.size()
    sw, sh = screen_size
    xs = [k.x for k in calibration.keys]
    ys = [k.y for k in calibration.keys]
    out = [k.key for k in calibration.keys if not (0 <= k.x < sw and 0 <= k.y < sh)]
    if out:
        return False, (
            f"有 {len(out)} 个键坐标超出屏幕（屏幕 {sw}×{sh}，坐标范围 "
            f"x {min(xs):.0f}~{max(xs):.0f}, y {min(ys):.0f}~{max(ys):.0f}、越界键 {out}）。"
            "请重新做「③ 四点校准」。"
        )
    return True, f"21 键坐标均在屏幕内（x {min(xs):.0f}~{max(xs):.0f}, y {min(ys):.0f}~{max(ys):.0f}）"


# ----------------------------------------------------------------------------
# 主窗口
# ----------------------------------------------------------------------------
class WindsongApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("风物之诗琴自动化工具 · 桌面版")
        self.geometry("1200x820")
        self.minsize(1000, 700)

        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._play_stop = threading.Event()
        self._play_thread: Optional[threading.Thread] = None
        self._busy = False
        self._playing = False          # 是否正在播放/自检（F7 切换用）
        self._hotkey = None            # keyboard 库句柄

        # 校准画布状态
        self._shot_bgr = None            # 全分辨率截图 (cv2 BGR)
        self._shot_scale = 1.0           # 显示缩放比例
        self._photo = None               # tk.PhotoImage（保持引用）
        self._corners: List[List[float]] = []  # 图像坐标 TL,TR,BR,BL
        self._drag_idx: Optional[int] = None
        self._cal: Optional[object] = None
        self._latency_lead: Dict[str, float] = {}   # 逐键提前量(秒)，来自延迟采样样本库
        self._audio_devices: list = []             # 可用的音频输入设备
        self._dev_label_to_index: Dict[str, int] = {}  # 下拉框标签 -> 设备索引
        self._dev_label_loopback: Dict[str, bool] = {}  # 下拉框标签 -> 是否回环(系统声音)

        # 浏览器编辑器本地服务（仅 127.0.0.1，用于「在浏览器微调」闭环）
        self._editor_server = None            # ThreadingHTTPServer 实例
        self._editor_port: Optional[int] = None
        self._editor_lock = threading.Lock()

        self._bp_ok, self._bp_msg = basic_pitch_available()

        self._build_ui()
        self.after(120, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._setup_hotkey()

        self.log("欢迎使用风物之诗琴自动化工具（桌面版）")
        if self._bp_ok:
            self.log("✓ basic_pitch 已就绪，可以开始「① 音频→MIDI」")
        else:
            self.log("✗ 未检测到 basic_pitch：" + self._bp_msg)
            self.log("  请通过项目目录下的 启动.bat 启动，或检查 .venv 是否已正确安装依赖。")

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self._nb = nb
        nb.add(self._frame_convert(), text=" ① 音频 → MIDI ")
        nb.add(self._frame_map(), text=" ② MIDI → 琴谱 ")
        nb.add(self._frame_calibrate(), text=" ③ 四点校准 ")
        nb.add(self._frame_latency(), text=" ④ 延迟采样 ")
        nb.add(self._frame_play(), text=" ⑤ 播放 ")

        logf = ttk.LabelFrame(self, text=" 日志 ")
        logf.pack(fill="both", padx=8, pady=(0, 8))
        self.log_text = tk.Text(logf, height=8, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, side="left")
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        sb.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=sb.set)

    def _frame_convert(self):
        f = ttk.Frame(self._nb, padding=16)
        self._var_audio = tk.StringVar()
        if self._bp_ok:
            tip = "上传/选择歌曲音频（MP3/WAV），用 basic-pitch 复音转写为 MIDI。"
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

        ttk.Label(f, text="转写完成后自动生成 data/output/song.mid，请到「② MIDI→琴谱」继续。",
                  foreground="#666").grid(row=4, column=0, columnspan=3, sticky="w")

        f.columnconfigure(1, weight=1)
        return f

    def _frame_map(self):
        f = ttk.Frame(self._nb, padding=16)
        self._var_midi = tk.StringVar(value=str(C.output_dir / "song.mid"))
        self._var_autotp = tk.BooleanVar(value=True)
        self._var_mono = tk.BooleanVar(value=True)
        self._var_melody_mode = tk.StringVar(value="high")
        self._var_oor = tk.StringVar(value="drop")   # drop=丢弃超音域, fold=折叠八度
        ttk.Label(f, text="把 MIDI 里的每个音符折叠/替换到 21 个白键，生成 events.json 琴谱。\n"
                         "本琴只有白键，默认会自动移调到最「白键友好」的调，避免旋律音高被改掉。",
                  wraplength=900, justify="left").grid(row=0, column=0, columnspan=3, sticky="w")

        ttk.Label(f, text="MIDI 文件：").grid(row=1, column=0, sticky="e", pady=(14, 4))
        ttk.Entry(f, textvariable=self._var_midi, width=80).grid(row=1, column=1, sticky="we", pady=(14, 4))
        ttk.Button(f, text="浏览…", command=self._browse_midi).grid(row=1, column=2, padx=4, pady=(14, 4))

        opt = ttk.Frame(f)
        opt.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # 「分层独立八度」：对标模拟器「启用自动八度偏移（各轨独立优化）」
        # 总开关是多选项；A / B 两个方案做成单选项（互斥，只能选一个）。
        lf = ttk.LabelFrame(f, text=" 分层独立八度（各层单独算八度，别让低音和旋律挤在同一段） ",
                            padding=8)
        lf.grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self._var_layered = tk.BooleanVar(value=False)
        self._var_layer_mode = tk.StringVar(value="pitch")
        ttk.Checkbutton(lf, text="开启分层独立八度（各层单独算八度）",
                        variable=self._var_layered,
                        command=self._update_layered_hint).pack(anchor="w")
        ttk.Label(lf, text="不勾就是以前的老做法（所有音一起折叠进 21 键）。勾上以后，"
                          "低音 / 中音 / 高音各自往 60~71 / 72~83 / 84~95 三段互不重叠的琴键上靠，"
                          "低音就不会被折到旋律区里把声音搅浑。",
                  foreground="#666", wraplength=880, justify="left").pack(anchor="w", pady=(2, 6))
        lay_row = ttk.Frame(lf)
        lay_row.pack(fill="x")
        ttk.Label(lay_row, text="分层方式：").pack(side="left")
        ttk.Radiobutton(lay_row, text="A 按音高分层（低/中/高各一段）",
                        value="pitch", variable=self._var_layer_mode,
                        command=self._update_layered_hint).pack(side="left")
        ttk.Radiobutton(lay_row, text="B 按 MIDI 音轨分层（每轨一个八度）",
                        value="track", variable=self._var_layer_mode,
                        command=self._update_layered_hint).pack(side="left", padx=(16, 0))
        self._lbl_layered = ttk.Label(lf, text="", wraplength=880, justify="left",
                                      foreground="#1a5fb4")
        self._lbl_layered.pack(anchor="w", pady=(4, 0))
        self._update_layered_hint()
        ttk.Checkbutton(opt, text="自动移调到白键友好调性（强烈建议开启）",
                        variable=self._var_autotp).pack(side="left")
        ttk.Checkbutton(opt, text="只保留主旋律（单音）", variable=self._var_mono).pack(side="left", padx=16)
        ttk.Label(opt, text="取音：").pack(side="left")
        ttk.Combobox(opt, textvariable=self._var_melody_mode, width=6, state="readonly",
                     values=["high", "loud"]).pack(side="left")
        ttk.Label(opt, text="(high=最高音/推荐, loud=最响)").pack(side="left", padx=(4, 12))
        ttk.Label(opt, text="超音域音：").pack(side="left")
        ttk.Combobox(opt, textvariable=self._var_oor, width=6, state="readonly",
                     values=["drop", "fold"]).pack(side="left")
        ttk.Label(opt, text="(drop=丢弃低音贝斯, fold=折叠八度)").pack(side="left", padx=4)

        row = ttk.Frame(f)
        row.grid(row=4, column=0, columnspan=3, sticky="w", pady=10)
        self._btn_map = ttk.Button(row, text="生成琴谱", command=self._on_map)
        self._btn_map.pack(side="left")
        self._btn_editor = ttk.Button(row, text="在浏览器中微调（编辑器）",
                                      command=self._on_open_editor)
        self._btn_editor.pack(side="left", padx=8)
        self._btn_notation = ttk.Button(row, text="导出按键谱(txt)",
                                        command=self._on_export_notation)
        self._btn_notation.pack(side="left")

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
        row2.grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 2))
        ttk.Label(row2, text="高音柔化：").pack(side="left")
        self._var_ceiling = tk.StringVar(value="C6(84)")
        cb_ceiling = ttk.Combobox(row2, textvariable=self._var_ceiling, width=8,
                                  state="readonly",
                                  values=["不限", "B6(95)", "A6(93)", "G6(91)", "C6(84)",
                                          "A5(81)", "G5(79)"])
        cb_ceiling.pack(side="left")
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
        self._lbl_soften_hint.grid(row=6, column=0, columnspan=3, sticky="w", pady=(0, 6))
        self._update_soften_hint()

        # 「音符整理」：起点/时值对齐拍网格（只动节奏与长短，不改音高旋律）
        row3 = ttk.Frame(f)
        row3.grid(row=7, column=0, columnspan=3, sticky="w", pady=(0, 2))
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
                       "只动节奏和长短，音高旋律完全不改。",
                  foreground="#666", wraplength=900, justify="left").grid(
            row=8, column=0, columnspan=3, sticky="w", pady=(0, 6))

        self._lbl_map_stats = ttk.Label(f, text="（尚未生成琴谱）", foreground="#666",
                                        wraplength=900, justify="left")
        self._lbl_map_stats.grid(row=9, column=0, columnspan=3, sticky="w")
        f.columnconfigure(1, weight=1)
        return f

    # ---- ② 「分层独立八度」的开关 + 方案（A / B 互斥单选）----
    def _update_layered_hint(self):
        """刷新「分层独立八度」下方的白话说明。"""
        if not bool(self._var_layered.get()):
            self._lbl_layered.configure(text="当前：老做法（所有音一起折叠进 21 键）。")
            return
        if self._var_layer_mode.get() == "pitch":
            self._lbl_layered.configure(
                text="A 按音高分三层：最低一层往 60~71（底行 Z~M）靠，"
                     "中间一层往 72~83（中行 A~J）靠，最高一层往 84~95（顶行 Q~U）靠。"
                     "三段互不重叠，低音不再闯进旋律区。")
        else:
            self._lbl_layered.configure(
                text="B 按 MIDI 音轨分层：每个音轨各挑一个八度偏移（多轨时各轨错开）。"
                     "谱子只有一个音轨时，效果和 A 一样。")

    def _update_soften_hint(self):
        """刷新柔化参数下方的白话提示（文案逻辑见 soften_hint_text）。"""
        self._lbl_soften_hint.configure(
            text=soften_hint_text(self._var_ceiling.get(), self._var_maxleap.get())
        )

    def _frame_calibrate(self):
        f = ttk.Frame(self._nb, padding=8)

        # ---------------- 弹奏方式：默认老方案，勾一下才开「原神键位优化」 ----------------
        mode_f = ttk.LabelFrame(f, text=" 弹奏方式（默认老方案，勾一下才切成键盘弹奏） ", padding=10)
        mode_f.pack(fill="x", pady=(0, 8))
        self._var_keyopt = tk.BooleanVar(value=False)

        key_row = ttk.Frame(mode_f)
        key_row.pack(fill="x")
        ttk.Checkbutton(
            key_row, text="原神键位优化（Q→U / A→J / Z→M，用键盘弹奏）",
            variable=self._var_keyopt, command=self._on_keyopt_change,
        ).pack(side="left", anchor="w")
        ttk.Label(key_row,
                  text="：底行 Z~M = C4~B4、中行 A~J = C5~B5、顶行 Q~U = C6~B6（低音在左、高音在右），"
                       "播放时直接敲键盘，不用截图也不用四点校准。不勾就是以前的老方案（截屏 + 鼠标点击），"
                       "这里不会改动你的屏幕校准文件。",
                  foreground="#666").pack(side="left", anchor="w")

        self._lbl_input_mode = ttk.Label(mode_f, text="", wraplength=940,
                                         justify="left", foreground="#1a5fb4")
        self._lbl_input_mode.pack(anchor="w", pady=(6, 0))
        self._sync_keyopt_from_file()

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="① 截取屏幕", command=self._on_screenshot).pack(side="left")
        ttk.Button(bar, text="② 计算 21 键并保存", command=self._on_compute_calibration).pack(side="left", padx=8)
        ttk.Button(bar, text="查看调试图", command=self._on_open_debug).pack(side="left")
        self._lbl_cal = ttk.Label(bar, text="先截屏，再把 4 个红点拖到琴键区四角（顺序：左上→右上→右下→左下）",
                                  foreground="#666")
        self._lbl_cal.pack(side="left", padx=12)

        self._canvas = tk.Canvas(f, bg="#222", height=600, highlightthickness=0)
        self._canvas.pack(fill="both", expand=True)
        self._canvas.bind("<Button-1>", self._on_canvas_press)
        self._canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        return f

    # ---- ③ 「原神键位优化」开关 ----
    def _update_keyopt_hint(self):
        if bool(self._var_keyopt.get()):
            tip = ("当前：原神键位优化已开启 —— 21 个白键直接就是 21 个字母键，播放时敲键盘（不是鼠标点）。"
                   "到 ⑤ 直接点「开始播放」即可；弹出来没声音就到 ⑤ 把「发送大写（Shift+字母）」勾上。"
                   "屏幕校准文件保持原样，取消勾选就回到鼠标点击的老方案。")
        else:
            tip = ("当前：老方案（屏幕校准）—— 需要「① 截取屏幕」+ 拖 4 个角点 + 计算 21 键并保存，"
                   "播放时用鼠标点琴键坐标。想改成直接敲键盘，就勾上面的「原神键位优化」。")
        self._lbl_input_mode.configure(text=tip)

    def _sync_keyopt_from_file(self):
        """历史兼容：老版本把键盘模式写进了 calibration.json（坐标全 0）。

        现在弹奏方式由勾选框决定、不再动文件，所以这里：
          · **绝不自动勾上「原神键位优化」** —— 不勾就必须是老方案；
          · 只提醒一句：磁盘上的校准是旧的键盘校准（坐标全 0），
            想用鼠标点击就重新「① 截取屏幕 → 计算 21 键并保存」覆盖它。
        """
        try:
            from piano_tool.player import load_calibration
            from piano_tool.keyboard_play import is_keyboard_calibration

            if is_keyboard_calibration(load_calibration(C.calibration_path)):
                self.log("ℹ 提示：磁盘上的校准文件还是旧版「键盘校准」（坐标全 0，鼠标点不了）。"
                         "想恢复鼠标点击的老方案，请在③ 重新「① 截取屏幕 → ② 计算 21 键并保存」。"
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

    def _frame_latency(self):
        f = ttk.Frame(self._nb, padding=16)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(5, weight=1)

        ttk.Label(f, text="逐键测量「点击 → 发声」的延迟，存成样本库（key_latency.json）。\n"
                          "播放时会按样本逐键提前补偿，让声音更准地落在节拍上。",
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
        self._btn_sample = ttk.Button(ctrl, text="▶ 开始逐键采样", command=self._on_sample_latency)
        self._btn_sample.pack(side="left", padx=(8, 0))
        self._lbl_device = ttk.Label(ctrl, text="", foreground="#1a5fb4")
        self._lbl_device.pack(side="left", padx=12)

        # 手动兜底
        manual = ttk.Frame(f)
        manual.grid(row=2, column=0, columnspan=5, sticky="w", pady=(4, 0))
        self._var_manual_latency = tk.IntVar(value=50)
        ttk.Label(manual, text="手动统一延迟：").pack(side="left")
        ttk.Spinbox(manual, from_=0, to=500, increment=5, width=6,
                    textvariable=self._var_manual_latency).pack(side="left")
        ttk.Label(manual, text="ms").pack(side="left", padx=(4, 8))
        ttk.Button(manual, text="保存为手动样本", command=self._on_save_manual_latency).pack(side="left")
        ttk.Label(manual, text="（音频采不到时，先估计一个统一延迟保存，后续再细调）",
                  foreground="#666").pack(side="left", padx=(8, 0))

        self._lbl_latency = ttk.Label(f, text="", foreground="#1a5fb4")
        self._lbl_latency.grid(row=3, column=0, columnspan=5, sticky="w", pady=(8, 0))

        ttk.Label(f, text="逐键结果（数值为延迟毫秒，越小越跟手）：").grid(row=4, column=0, sticky="w", pady=(6, 2))

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

        self._on_refresh_devices()
        return f

    def _clear_latency_text(self):
        self._lat_text.configure(state="normal")
        self._lat_text.delete("1.0", "end")
        self._lat_text.configure(state="disabled")

    def _on_refresh_devices(self):
        try:
            from piano_tool.latency_probe import list_capture_devices, default_loopback_device
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
            # 默认选中：优先默认输出对应的「系统声音(回环)」，否则任意系统声音，否则第一个
            default_out = default_loopback_device()
            init_label = None
            if default_out is not None:
                for lab, idx in self._dev_label_to_index.items():
                    if idx == default_out and self._dev_label_loopback.get(lab):
                        init_label = lab
                        break
            if init_label is None:
                for lab in labels:
                    if self._dev_label_loopback.get(lab):
                        init_label = lab
                        break
            if init_label is None:
                init_label = labels[0]
            self._combo_audio_dev.set(init_label)
            n_loop = sum(1 for d in devs if d.get("loopback"))
            self._lbl_device.configure(
                text=f"共 {len(devs)} 个捕获设备（含 {n_loop} 个「系统声音(回环)」），已自动选中推荐项（可下拉手动切换）",
                foreground="#1a5fb4",
            )
            self.log("音频捕获设备：" + "，".join(d["name"] for d in devs[:8]) or "无")
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
        self._set_busy(False, self._btn_sample, "▶ 开始逐键采样")
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
        self._lbl_latency.configure(text="采样中… 请保持模拟器在前、声音开启", foreground="#1a5fb4")
        self.iconify()
        threading.Thread(target=self._latency_worker, daemon=True).start()

    def _latency_worker(self):
        try:
            from piano_tool.calibrate_latency import (
                load_latency_profile, profile_to_lead_seconds, sample_all_keys,
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
            self._q.put(("log", f"开始逐键采样（监听设备：{monitor.device_name}），请保持模拟器在前、声音开启"))
            extra = {"key_fn": (lambda kk: tap_key(kk, upper=bool(self._var_upper.get())))
                     if keyboard_mode else None}
            result = sample_all_keys(
                cal, monitor=monitor,
                on_each=lambda k, lat: self._q.put(("latency_progress", (k, lat))),
                **extra,
            )
            out = save_latency_profile(result, C.latency_profile_path, device_name=monitor.device_name)
            prof = load_latency_profile(out)
            self._latency_lead = profile_to_lead_seconds(prof) if prof else {}
            none_keys = [k for k, v in result.items() if v is None]
            if none_keys:
                self._q.put(("log", f"⚠ 有 {len(none_keys)} 个键未检测到声音（{', '.join(none_keys)}）。"
                                     "请检查音量/回环设备，或改用手动统一延迟。"))
            self._q.put(("log", f"✓ 采样完成，已保存到 {C.latency_profile_path.name}"
                               f"（均值 {prof.get('mean_ms') if prof else '—'} ms，{len(self._latency_lead)} 个键有效）"))
            self._q.put(("latency_done", result))
        except Exception as exc:  # noqa: BLE001
            logger.exception("latency sampling failed")
            self._q.put(("log", f"✗ 采样失败：{exc}"))
            self._q.put(("latency_done", None))

    def _on_q_latency_progress(self, payload):
        k, lat = payload
        line = f"{k}: {('%.1f ms' % (lat * 1000)) if lat is not None else '未检测到（跳过）'}\n"
        self._lat_text.configure(state="normal")
        self._lat_text.insert("end", line)
        self._lat_text.configure(state="disabled")
        self._lat_text.see("end")

    def _on_q_latency_done(self, payload):
        self._busy = False
        self._set_busy(False, self._btn_sample, "▶ 开始逐键采样")
        try:
            self.deiconify()
        except Exception:  # noqa: BLE001
            pass
        if payload:
            valid = [v for v in payload.values() if v is not None]
            if valid:
                mean_ms = sum(valid) / len(valid) * 1000
                self._lbl_latency.configure(
                    text=f"✓ 样本库就绪：{len(valid)}/21 键有效，均值 {mean_ms:.1f} ms",
                    foreground="#1a7a1a")
            else:
                self._lbl_latency.configure(
                    text="✗ 21 个键都未检测到声音。请先用「测试音频」确认声音是否进入本程序。",
                    foreground="#c0392b")
        else:
            self._lbl_latency.configure(text="采样未完成，可重试或检查音频设备", foreground="#c0392b")

    def _on_save_manual_latency(self):
        if not Path(C.calibration_path).exists():
            messagebox.showwarning("提示", "尚未校准，请先完成「③ 四点校准」")
            return
        try:
            from piano_tool.calibrate_latency import (build_profile_from_constant,
                                                       load_latency_profile,
                                                       profile_to_lead_seconds)
            from piano_tool.player import load_calibration

            cal = load_calibration(C.calibration_path)
            ms = float(self._var_manual_latency.get())
            out = build_profile_from_constant(ms, cal, C.latency_profile_path)
            prof = load_latency_profile(out)
            self._latency_lead = profile_to_lead_seconds(prof) if prof else {}
            self._clear_latency_text()
            self._lat_text.configure(state="normal")
            for k in sorted(prof["latencies"].keys()):
                self._lat_text.insert("end", f"{k}: {prof['latencies'][k]} ms（手动统一）\n")
            self._lat_text.configure(state="disabled")
            self._lbl_latency.configure(
                text=f"✓ 已保存手动样本库：统一 {ms:.0f} ms，播放时会按此逐键提前",
                foreground="#1a7a1a")
            self.log(f"✓ 手动延迟样本库已保存：{out}，{len(self._latency_lead)} 个键")
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
        self._var_testkey = tk.StringVar()

        ttk.Checkbutton(f, text="试运行（只打印计划，不真实点击）",
                        variable=self._var_dry,
                        command=self._on_toggle_dry).grid(row=1, column=0, columnspan=4, sticky="w", pady=(10, 2))
        self._lbl_mode = ttk.Label(f, text="", foreground="#c0392b")
        self._lbl_mode.grid(row=1, column=1, columnspan=3, sticky="w", pady=(10, 2))
        ttk.Checkbutton(f, text="键盘方式下发送大写（Shift+字母）：模拟器只认大写字母时才勾它",
                        variable=self._var_upper).grid(row=1, column=0, sticky="w", pady=(10, 2))
        ttk.Label(f, text="倍速：").grid(row=2, column=0, sticky="e")
        ttk.Spinbox(f, from_=0.25, to=4.0, increment=0.25, width=6,
                    textvariable=self._var_speed).grid(row=2, column=1, sticky="w")
        ttk.Label(f, text="点击间隔(ms)：").grid(row=2, column=2, sticky="e", padx=(16, 0))
        ttk.Spinbox(f, from_=0, to=500, increment=10, width=6,
                    textvariable=self._var_delay).grid(row=2, column=3, sticky="w")
        ttk.Label(f, text="开始倒计时(秒)：").grid(row=3, column=0, sticky="e", pady=(8, 0))
        ttk.Spinbox(f, from_=0, to=30, increment=1, width=6,
                    textvariable=self._var_countdown).grid(row=3, column=1, sticky="w", pady=(8, 0))
        ttk.Label(f, text="提前量(ms)：").grid(row=3, column=2, sticky="e", padx=(16, 0), pady=(8, 0))
        ttk.Spinbox(f, from_=0, to=500, increment=10, width=6,
                    textvariable=self._var_latency).grid(row=3, column=3, sticky="w", pady=(8, 0))
        ttk.Label(f, text="提前量：点击比节拍提前触发，抵消「点击→发声」的延迟，连音更跟手。",
                  foreground="#666").grid(row=4, column=0, columnspan=4, sticky="w")
        # 按住时值（还原音符长短/节奏）
        ttk.Checkbutton(f, text="按音符时值按住（还原长短/节奏）",
                        variable=self._var_hold).grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(f, text="最小按住(ms)：").grid(row=5, column=2, sticky="e", pady=(8, 0))
        ttk.Spinbox(f, from_=5, to=300, increment=5, width=6,
                    textvariable=self._var_holdmin).grid(row=5, column=3, sticky="w", pady=(8, 0))

        row = ttk.Frame(f)
        row.grid(row=6, column=0, columnspan=4, sticky="w", pady=14)
        self._btn_play = ttk.Button(row, text="▶ 开始播放", command=self._on_play)
        self._btn_play.pack(side="left")
        self._btn_stop = ttk.Button(row, text="■ 停止", command=self._on_stop, state="disabled")
        self._btn_stop.pack(side="left", padx=8)
        ttk.Separator(row, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Label(row, text="自检键：").pack(side="left")
        self._combo_testkey = ttk.Combobox(row, textvariable=self._var_testkey, width=8,
                                           values=[str(k["key"]) for k in WHITE_KEYS], state="readonly")
        self._combo_testkey.pack(side="left")
        if WHITE_KEYS:
            self._combo_testkey.current(0)
        self._btn_test = ttk.Button(row, text="🎯 点击自检（只点一下）",
                                    command=self._on_self_test)
        self._btn_test.pack(side="left", padx=8)

        self._lbl_play = ttk.Label(f, text="（空闲）", foreground="#666")
        self._lbl_play.grid(row=7, column=0, columnspan=4, sticky="w")
        ttk.Label(f, text="快捷键 F7：开始 / 停止（全局热键，本窗口最小化时同样有效）",
                  foreground="#1a5fb4").grid(row=8, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self._on_toggle_dry()
        return f

    def _on_toggle_dry(self):
        if self._var_dry.get():
            self._lbl_mode.configure(text="当前：试运行（不会点击）", foreground="#666")
        else:
            self._lbl_mode.configure(
                text="当前：真实点击模式 —— 开始后本窗口会自动最小化，请把浏览器模拟器置于最前",
                foreground="#c0392b",
            )

    # --------------------------------------------------------------- 日志
    def log(self, msg: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

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
        """F7：播放中就停止，空闲就开始。"""
        if self._playing:
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
        self._set_busy(True, self._btn_convert, "转写中…（可能需要几十秒到几分钟）")
        self.log(f"开始转写：{src}")
        threading.Thread(target=self._convert_worker, args=(src,), daemon=True).start()

    def _convert_worker(self, src: str):
        """音频 → MIDI。

        一律走乐器的默认阈值。以前这里会判断「这个音频是不是文本发声合成的语音」
        （语音起音不清晰，要放宽阈值），随 TTS 模块一起去掉了：
        普通音乐走语音阈值会被当成噪声、漏掉真音符。
        """
        try:
            from piano_tool.audio_to_midi import audio_to_midi

            out = C.output_dir / "song.mid"
            count, out_path = audio_to_midi(src, out)
            self._q.put(("log", f"✓ 转写完成：{count} 个音符 -> {out_path}"))
            self._q.put(("convert_done", str(out_path)))
        except Exception as exc:  # noqa: BLE001
            logger.exception("convert failed")
            self._q.put(("log", f"✗ 转写失败：{exc}"))
            self._q.put(("convert_done", None))

    def _on_q_convert_done(self, _payload):
        self._set_busy(False, self._btn_convert, "开始转写")
        self._var_midi.set(str(C.output_dir / "song.mid"))
        self.log("请切换到「② MIDI→琴谱」生成琴谱。")

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

            events, stats = map_midi_to_events(
                src,
                auto_transpose=bool(self._var_autotp.get()),
                mono=bool(self._var_mono.get()),
                melody_mode=self._var_melody_mode.get() or "high",
                fold_out_of_range=(self._var_oor.get() == "fold"),
                layered_octave=bool(self._var_layered.get()),
                layer_mode=self._var_layer_mode.get() or "pitch",
            )
            if stats.get("layered"):
                self._q.put(("log", "已开启分层独立八度：" + "；".join(
                    f"第{i+1}层 原始{fmt_range(r['orig_range'])}→{fmt_range(r['mapped_range'])}"
                    f"（带{fmt_range(r['band'])}，偏移{r['shift']:+d}半音）"
                    for i, r in enumerate(stats["layers"]))))
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
            # 音域体检：原始音高范围 → 移调 → 落进 21 键的范围，
            # 对标模拟器「原始范围 / 合并偏移 / 调整后范围」那块面板
            o = fmt_range(stats.get("orig_range"))
            m = fmt_range(stats.get("mapped_range"))
            span = stats.get("white_span") or [LOWEST_MIDI, HIGHEST_MIDI]
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
            self._lbl_map_stats.configure(
                text=(f"音域：原始 {o} → 移调 {stats['transpose']:+d} 半音 → 落到 {m}"
                      f"（琴键 {span[0]}~{span[1]}，用了 {stats['keys_used']} 个键）"
                      f"{out_of_span}\n"
                      f"共 {stats['total']} 音符 · 白键命中 "
                      f"{('%.1f%%' % (rate*100)) if rate is not None else '—'} · "
                      f"音高被改动 {stats['black_replaced']} · 八度折叠 {stats['octave_folded']} · "
                      f"清理毛刺 {stats['dropped']}"
                      f"{lay_txt}"),
                foreground="#1a7a1a" if not out_of_span else "#c0392b",
            )
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
                f"合并碎片 {stats['merged']}"
            )
            self._lbl_map_stats.configure(
                text=(f"整理后：{stats['out']} 音符 · BPM {stats['bpm']:.0f} · "
                      f"网格 {stats['grid']*1000:.0f}ms · 合并碎片 {stats['merged']}"),
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

    def _start_editor_server(self) -> int:
        """启动（或复用）编辑器本地服务，返回端口。线程安全、幂等。"""
        with self._editor_lock:
            if self._editor_server is not None:
                return int(self._editor_port)  # type: ignore
            server = ThreadingHTTPServer(("127.0.0.1", 0), _EditorHandler)
            port = server.server_address[1]
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

            from piano_tool.imio import imread_unicode, imwrite_unicode

            C.data_dir.mkdir(parents=True, exist_ok=True)
            full = str(C.data_dir / "screenshot.png")
            with mss.mss() as sct:
                sct.shot(output=full)
            img = imread_unicode(full)
            if img is None:
                raise RuntimeError("截屏失败，无法读取图像")
            self._shot_bgr = img
            h, w = img.shape[:2]

            # 缩放到画布可容纳的大小
            canvas_w = max(self._canvas.winfo_width() - 4, 400)
            canvas_h = max(self._canvas.winfo_height() - 4, 300)
            scale = min(canvas_w / w, canvas_h / h, 1.0)
            self._shot_scale = scale
            disp = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
            preview = C.data_dir / "screenshot_preview.png"
            imwrite_unicode(preview, disp)

            self._photo = tk.PhotoImage(file=str(preview))
            self._canvas.delete("all")
            self._canvas.create_image(0, 0, image=self._photo, anchor="nw")

            self._corners = place_default_corners(w, h)
            self._cal = None
            self._redraw_overlay()
            self._lbl_cal.configure(text="已截屏。请把 4 个红点拖到琴键区四角，然后点「计算 21 键并保存」。",
                                     foreground="#1a5fb4")
            self.log(f"截屏完成：{w}×{h}，显示缩放 {scale:.3f}")
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
                           font=("Microsoft YaHei", 10, "bold"), tags="overlay")
        # 已有校准结果：画 21 键
        if self._cal is not None:
            for k in self._cal.keys:
                x, y = self._img2disp(k.x, k.y)
                cv.create_oval(x - 4, y - 4, x + 4, y + 4, fill="#2ec27e", outline="",
                               tags="overlay")
                cv.create_text(x + 7, y - 6, text=k.key, fill="#2ec27e",
                               font=("Consolas", 9, "bold"), tags="overlay")

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
            cal = build_calibration(self._corners, image_size=[w, h])
            save_calibration(cal, C.calibration_path)
            # 这次存的是「屏幕坐标」校准，若旧文件还是键盘方式就同步一下勾选框（只读不改文件）
            self._sync_keyopt_from_file()
            draw_debug_image(self._shot_bgr, cal, C.debug_image)
            self._cal = cal
            self._redraw_overlay()
            self._lbl_cal.configure(
                text=f"✓ 校准完成：21 键坐标已保存到 {C.calibration_path.name}（绿点即各键点击位置）",
                foreground="#1a7a1a",
            )
            self.log("✓ 校准完成。可点「查看调试图」人工核对 21 个绿点是否都落在键上。")
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
        ok, msg = check_coords_on_screen(cal)
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
        self._play_stop.clear()
        self._playing = True
        self._set_busy(True, self._btn_play, "播放中…（F7 停止）")
        self._btn_stop.configure(state="normal")
        if not dry:
            # 真实点击：先最小化本窗口，避免窗口挡住模拟器导致点在自己身上
            self.iconify()
            self.log("本窗口已最小化到任务栏，请在倒计时内把浏览器模拟器置于最前。")
        threading.Thread(
            target=self._play_worker,
            args=(events_path, dry, float(self._var_speed.get()),
                  int(self._var_delay.get()), int(self._var_countdown.get()),
                  float(self._var_latency.get()),
                  bool(self._var_hold.get()), float(self._var_holdmin.get()),
                  mode),
            daemon=True,
        ).start()

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
                target=self._selftest_key_worker, args=(key, int(self._var_countdown.get())),
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

    def _selftest_key_worker(self, key: str, countdown: int):
        """键盘映射方式下的自检：倒计时后敲一下对应的字母键。"""
        try:
            from piano_tool.keyboard_play import tap_key

            counts = max(countdown, 3)
            for i in range(counts, 0, -1):
                if self._play_stop.is_set():
                    self._q.put(("selftest_done", "自检已取消"))
                    return
                self._q.put(("play_status", f"自检倒计时 {i} 秒…（请把模拟器置于最前）"))
                time.sleep(1.0)
            upper = bool(self._var_upper.get())
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
                     input_mode: str = "screen"):
        try:
            from piano_tool.player import load_calibration, load_events, play

            if countdown > 0:
                for i in range(countdown, 0, -1):
                    if self._play_stop.is_set():
                        self._q.put(("play_done", "已取消"))
                        return
                    self._q.put(("play_status", f"倒计时 {i} 秒后开始…"))
                    time.sleep(1.0)

            events = load_events(events_path)
            cal = load_calibration(C.calibration_path)
            coord0 = {k.key: (k.x, k.y) for k in cal.keys}
            first = events[0] if events else None
            if str(input_mode) == "keyboard":
                mode = "键盘弹奏（Q~U / A~J / Z~M）" + (" · 按住时值" if hold else "")
            elif dry_run:
                mode = "试运行（不真实点击）"
            else:
                mode = "真实点击 · 按住时值" if hold else "真实点击"
            # 载入逐键延迟样本库（若有），作为逐键提前量
            lead = None
            if Path(C.latency_profile_path).exists():
                try:
                    from piano_tool.calibrate_latency import load_latency_profile, profile_to_lead_seconds

                    prof = load_latency_profile(C.latency_profile_path)
                    if prof:
                        lead = profile_to_lead_seconds(prof)
                        self._latency_lead = lead
                        self._q.put(("log", f"已载入逐键延迟样本库：{len(lead)} 个键，均值 {prof.get('mean_ms')} ms"))
                except Exception as exc:  # noqa: BLE001
                    self._q.put(("log", f"⚠ 延迟样本库读取失败，改用全局提前量：{exc}"))
            self._q.put(("log", f"开始播放：{len(events)} 个音符，模式：{mode}"))
            if first is not None:
                c = coord0.get(first.key, (0, 0))
                self._q.put(("log", f"第一个音 t={first.time:.3f}s 键={first.key} 坐标=({c[0]:.0f}, {c[1]:.0f})"))
            if not dry_run:
                self._q.put(("log", f"按住时值：{'开启' if hold else '关闭'}（最小按住 {hold_min_ms:.0f}ms）"))
            self._q.put(("play_status",
                         f"播放中…（{len(events)} 个音符，{mode}）"))
            devs = play(
                events, cal, speed=speed, delay_ms=delay_ms, dry_run=dry_run,
                stop_check=self._play_stop.is_set,
                latency_comp_ms=latency_ms,
                latency_lead=lead,
                hold=hold, hold_min_ms=hold_min_ms,
                input_mode=input_mode,
            )
            if devs:
                import statistics

                avg = statistics.mean(devs) * 1000
                self._q.put(("log", f"✓ 播放结束：{len(devs)} 次{'模拟点击' if not dry_run else '计划音符'}，"
                                    f"平均时间偏差 {avg:.1f} ms"))
            else:
                self._q.put(("log", "播放结束（未产生点击）"))
            self._q.put(("play_done", "播放完成" if not self._play_stop.is_set() else "已停止"))
        except Exception as exc:  # noqa: BLE001
            logger.exception("play failed")
            self._q.put(("log", f"✗ 播放失败：{exc}"))
            self._q.put(("play_done", f"错误：{exc}"))

    def _on_stop(self):
        self._play_stop.set()
        self.log("已请求停止（若正在真实点击，也可把鼠标甩到左上角紧急停止）")

    def _on_q_play_status(self, msg):
        self._lbl_play.configure(text=msg + " · 按 F7 停止", foreground="#1a5fb4")

    def _on_q_play_done(self, msg):
        self._playing = False
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
        self._play_stop.set()
        self._stop_editor_server()
        if self._hotkey is not None:
            try:
                self._hotkey.unhook_all_hotkeys()
            except Exception:  # noqa: BLE001
                pass
        self.destroy()


def main():
    app = WindsongApp()
    app.mainloop()


if __name__ == "__main__":
    main()
