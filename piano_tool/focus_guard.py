"""焦点守卫（focus guard）——注入按键/点击只给「当前前台窗口」。

为什么需要它
------------
Windows 上pyautogui 的按键注入（SendInput / keybd_event）投递给**当前前台窗口**。
游戏不在前台时，它收不到任何按键 —— 这是系统机制，本身不是 bug。
真正的问题是老代码**根本不看焦点，照发不误**：焦点一旦跳到浏览器，
按键就全落在浏览器上（网页快捷键、输入框字母、页面滚动被乱按一通），
听起来像「弹奏还在继续」，游戏却一声不吭。

提供的东西
----------
    capture_target()      抓当前前台窗口作为目标（pid + 标题 + 进程名）
    target_matches(...)   纯逻辑判断：当前前台是不是目标（先比 pid，再比标题/进程名）
    make_checker(...)     给播放循环用的检查函数，失焦时返回 (False, 当前窗口标题)
    activate(...)         把焦点抢回目标窗口（播放前一键切回游戏）
    save_target/load_target  存进 data/focus_target.json，抓一次之后长期有效

非 Windows / 没装 pywin32 时全部安全降级（拿不到就当「不检查」），
绝不会让播放流程崩掉。
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

from .config import PROJECT_ROOT

logger = logging.getLogger("piano_tool.focus_guard")

TARGET_PATH = PROJECT_ROOT / "data" / "focus_target.json"

__all__ = [
    "FocusTarget",
    "TARGET_PATH",
    "activate",
    "capture_target",
    "capture_window_at",
    "cursor_pos",
    "describe_window",
    "diagnose_target",
    "get_foreground",
    "load_target",
    "make_checker",
    "process_elevation",
    "root_window_at",
    "save_target",
    "system_dpi_scale",
    "target_matches",
    "title_matches",
    "virtual_screen_rect",
]


@dataclass
class FocusTarget:
    """要弹奏的目标窗口。pid 最可靠，标题/进程名做兜底。"""

    pid: int = 0
    title: str = ""
    proc: str = ""

    def is_empty(self) -> bool:
        return not (self.pid or (self.title or "").strip() or (self.proc or "").strip())

    @classmethod
    def from_dict(cls, data) -> "FocusTarget":
        data = data or {}
        return cls(
            pid=int(data.get("pid") or 0),
            title=str(data.get("title") or ""),
            proc=str(data.get("proc") or ""),
        )


# ---------------------------------------------------------------------------
# 纯逻辑（不碰 Win32，测试直接覆盖这几段）
# ---------------------------------------------------------------------------
def title_matches(title, keyword) -> bool:
    """窗口标题是否命中关键字。关键字为空 = 不检查，一律命中。"""
    kw = str(keyword or "").strip()
    if not kw:
        return True
    return kw.lower() in str(title or "").lower()


def target_matches(target: Optional[FocusTarget], pid: int = 0, title: str = "",
                   proc: str = "") -> bool:
    """当前前台窗口（pid/title/proc）算不算目标窗口。

    判定顺序：pid 相同 → 标题含关键字 → 进程名相同 → 否则 False。
    pid 最稳（游戏内标题常带动态后缀，标题对不上；进程名则可能多个实例相同）。
    target 为空 = 没设目标 = 不检查，返回 True。
    """
    if target is None or target.is_empty():
        return True
    if target.pid and int(pid or 0) == int(target.pid):
        return True
    if target.title and title_matches(title, target.title):
        return True
    tp = str(target.proc or "").strip().lower()
    if tp and str(proc or "").strip().lower() == tp:
        return True
    return False


# ---------------------------------------------------------------------------
# Win32 部分（拿不到就降级）
# ---------------------------------------------------------------------------
def _win():
    """返回 (win32gui, win32api, win32process)，任何一步失败都返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        import win32api  # noqa: WPS433
        import win32gui  # noqa: WPS433
        import win32process  # noqa: WPS433

        return win32gui, win32api, win32process
    except Exception:  # noqa: BLE001  没装 pywin32 / 导入失败
        return None


def _proc_name(pid: int) -> str:
    """进程名（genshinimpact.exe）；拿不到就空字符串。"""
    mods = _win()
    if not mods or not pid:
        return ""
    _, win32api, win32process = mods
    handle = None
    try:
        handle = win32api.OpenProcess(0x1000 | 0x400, False, int(pid))
        path = win32process.GetModuleFileNameEx(handle, 0)
        return os.path.basename(str(path)).lower()
    except Exception:  # noqa: BLE001
        return ""
    finally:
        if handle:
            try:
                win32api.CloseHandle(handle)
            except Exception:  # noqa: BLE001
                pass


def get_foreground() -> Tuple[int, str, str]:
    """当前前台窗口的 (pid, 标题, 进程名)；拿不到返回 (0, "", "")。"""
    mods = _win()
    if not mods:
        return 0, "", ""
    win32gui, _, win32process = mods
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return 0, "", ""
        title = str(win32gui.GetWindowText(hwnd) or "")
        try:
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:  # noqa: BLE001
            pid = 0
        return int(pid or 0), title, _proc_name(int(pid or 0))
    except Exception:  # noqa: BLE001
        return 0, "", ""


def capture_target() -> FocusTarget:
    """把当前前台窗口抓成目标（游戏在前台时点一下这个按钮）。"""
    pid, title, proc = get_foreground()
    return FocusTarget(pid=pid, title=title, proc=proc)


# ---------------------------------------------------------------------------
# 瞄准镜选窗：屏幕坐标 -> 该位置的顶层窗口（不读窗口内容，只取句柄元数据）
# ---------------------------------------------------------------------------
def cursor_pos() -> Tuple[int, int]:
    """当前鼠标的屏幕坐标 (x, y)；拿不到返回 (0, 0)。"""
    mods = _win()
    if not mods:
        return 0, 0
    win32gui, _, _ = mods
    try:
        x, y = win32gui.GetCursorPos()
        return int(x), int(y)
    except Exception:  # noqa: BLE001
        return 0, 0


def root_window_at(x: int, y: int) -> int:
    """屏幕坐标 (x, y) 处的**顶层**窗口句柄；拿不到返回 0。

    WindowFromPoint 可能命中按钮/输入框等子窗口，统一用 GetAncestor(GA_ROOT)
    上溯到顶层窗口，和焦点守卫按窗口判定的粒度保持一致。
    只查询系统的窗口层级信息，不读取窗口画面或内容。
    """
    mods = _win()
    if not mods:
        return 0
    win32gui, _, _ = mods
    try:
        hwnd = win32gui.WindowFromPoint((int(x), int(y)))
        if not hwnd:
            return 0
        try:
            root = win32gui.GetAncestor(hwnd, 2)  # GA_ROOT = 2
            if root:
                hwnd = root
        except Exception:  # noqa: BLE001  老系统没有 GetAncestor 时退回原句柄
            pass
        return int(hwnd)
    except Exception:  # noqa: BLE001
        return 0


def describe_window(hwnd: int) -> Tuple[int, str, str]:
    """窗口句柄 -> (pid, 标题, 进程名)；拿不到返回 (0, "", "")。"""
    mods = _win()
    if not mods or not hwnd:
        return 0, "", ""
    win32gui, _, win32process = mods
    try:
        title = str(win32gui.GetWindowText(hwnd) or "")
        try:
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:  # noqa: BLE001
            pid = 0
        return int(pid or 0), title, _proc_name(int(pid or 0))
    except Exception:  # noqa: BLE001
        return 0, "", ""


def capture_window_at(x: Optional[int] = None, y: Optional[int] = None):
    """瞄准镜松手时调用：坐标缺省就读当前鼠标位置，返回该点的顶层窗口目标。

    拿到的是空窗口/桌面（无标题无进程）时返回 None。
    """
    if x is None or y is None:
        x, y = cursor_pos()
    hwnd = root_window_at(int(x), int(y))
    if not hwnd:
        return None
    pid, title, proc = describe_window(hwnd)
    target = FocusTarget(pid=pid, title=title, proc=proc)
    return None if target.is_empty() else target


def virtual_screen_rect() -> Tuple[int, int, int, int]:
    """所有显示器联合虚拟屏幕的 (x, y, 宽, 高)，用于全屏瞄准覆盖窗。"""
    mods = _win()
    if not mods:
        return 0, 0, 1920, 1080
    _gui, win32api, _proc = mods
    try:
        x = int(win32api.GetSystemMetrics(76))   # SM_XVIRTUALSCREEN
        y = int(win32api.GetSystemMetrics(77))   # SM_YVIRTUALSCREEN
        w = int(win32api.GetSystemMetrics(78))   # SM_CXVIRTUALSCREEN
        h = int(win32api.GetSystemMetrics(79))   # SM_CYVIRTUALSCREEN
        if w and h:
            return x, y, w, h
    except Exception:  # noqa: BLE001
        pass
    return 0, 0, int(win32api.GetSystemMetrics(0)), int(win32api.GetSystemMetrics(1))


def list_windows(keyword: str = "") -> list:
    """列出可见的顶层窗口 [(hwnd, 标题, pid)]，标题含关键字的优先排在前面。"""
    mods = _win()
    if not mods:
        return []
    win32gui, _, win32process = mods
    found = []

    def _cb(hwnd, _extra):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = str(win32gui.GetWindowText(hwnd) or "")
            if not title:
                return
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            found.append((int(hwnd), title, int(pid or 0)))
        except Exception:  # noqa: BLE001
            return

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:  # noqa: BLE001
        return found
    kw = str(keyword or "").strip().lower()
    if kw:
        found.sort(key=lambda w: (kw not in w[1].lower(),))
    return found


def activate(target: Optional[FocusTarget]) -> bool:
    """把焦点抢回目标窗口（最小化的先恢复）。成功返回 True。"""
    mods = _win()
    if not mods or target is None:
        return False
    win32gui, _, win32process = mods
    handles = []
    try:
        if target.pid:
            def _cb(hwnd, _extra):
                try:
                    _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
                    if int(pid or 0) == int(target.pid) and win32gui.IsWindowVisible(hwnd):
                        handles.append(hwnd)
                except Exception:  # noqa: BLE001
                    return
            win32gui.EnumWindows(_cb, None)
        if not handles:
            kw = str(target.title or "").strip().lower()
            for hwnd, title, _pid in list_windows():
                if kw and kw in title.lower():
                    handles.append(hwnd)
        if not handles:
            return False
        hwnd = handles[0]
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE
        win32gui.SetForegroundWindow(hwnd)
        return True
    except Exception:  # noqa: BLE001
        logger.debug("activate failed", exc_info=True)
        return False


def make_checker(target: Optional[FocusTarget] = None,
                 keyword: str = "") -> Optional[Callable[[], Tuple[bool, str]]]:
    """生成播放循环用的焦点检查函数。

    没给 target 也没给关键字 → 返回 None（调用方按「不检查」处理，
    这样默认行为和以前完全一样，不打扰现有用法）。
    """
    kw = str(keyword or "").strip()

    def check() -> Tuple[bool, str]:
        pid, title, proc = get_foreground()
        if not pid and not title:
            # 取不到前台窗口（系统限制/权限），别因为这个把用户的演奏卡住
            return True, ""
        if target is not None and not target.is_empty():
            return target_matches(target, pid, title, proc), title
        return title_matches(title, kw), title

    if (target is None or target.is_empty()) and not kw:
        return None
    return check


# ---------------------------------------------------------------------------
# 持久化：抓一次，之后长期有效
# ---------------------------------------------------------------------------
def save_target(target: FocusTarget, path: Path = TARGET_PATH) -> bool:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(asdict(target), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except Exception:  # noqa: BLE001
        logger.debug("save focus target failed", exc_info=True)
        return False


def load_target(path: Path = TARGET_PATH) -> Optional[FocusTarget]:
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    target = FocusTarget.from_dict(data)
    return None if target.is_empty() else target


# ---------------------------------------------------------------------------
# 「目标窗口体检」——直接回答「为什么这台机器网页能弹、游戏没反应」
# 全部是只读查询，不读取、不修改目标程序的任何内容：
#   * 目标进程的权限级别（令牌 TokenElevation）—— UIPI 会不会拦住输入
#   * 系统 DPI 缩放 —— 鼠标模式的坐标会不会整体偏移
#   * 输入注入后端是否可用（SendInput / 回退 pyautogui）
# ---------------------------------------------------------------------------
def _sec():
    """返回 (win32api, win32security)；拿不到返回 None（非 Windows / 没装 pywin32）。"""
    if sys.platform != "win32":
        return None
    try:
        import win32api  # noqa: WPS433
        import win32security  # noqa: WPS433

        return win32api, win32security
    except Exception:  # noqa: BLE001
        return None


def process_elevation(pid: int) -> str:
    """进程的权限级别："elevated"（管理员/高完整性）/ "normal" / "unknown"。

    只打开进程、读它令牌上的 TokenElevation 一个标志位，不读内存、不改内容；
    拿不到（权限不足 / 进程已退出 / 没装 pywin32）返回 "unknown"。
    """
    mods = _sec()
    if not mods or not pid:
        return "unknown"
    win32api, win32security = mods
    handle = None
    try:
        # PROCESS_QUERY_LIMITED_INFORMATION (0x1000)：只要基本信息，权限要求最低
        handle = win32api.OpenProcess(0x1000, False, int(pid))
        token = win32security.OpenProcessToken(handle, win32security.TOKEN_QUERY)
        value = win32security.GetTokenInformation(token, win32security.TokenElevation)
        if isinstance(value, (tuple, list)):
            value = value[0] if value else 0
        return "elevated" if int(value) else "normal"
    except Exception:  # noqa: BLE001
        return "unknown"
    finally:
        if handle:
            try:
                win32api.CloseHandle(handle)
            except Exception:  # noqa: BLE001
                pass


def system_dpi_scale() -> float:
    """系统 DPI 缩放比例（100% = 1.0）；拿不到返回 1.0。

    注意：进程若不是「DPI 感知」的，Windows 会把 DPI 查询结果折算成 96（＝100%），
    测出来永远是 1.0。所以这里先让本进程按物理像素读屏（与 pyautogui / inject
    播放时的状态一致）再读取，保证数字真实。
    """
    if sys.platform != "win32":
        return 1.0
    try:
        from . import inject

        inject.ensure_dpi_aware()
    except Exception:  # noqa: BLE001
        pass
    try:
        import ctypes  # noqa: WPS433

        user32 = ctypes.windll.user32
        dpi = 0
        try:
            dpi = int(user32.GetDpiForSystem())   # Win10 1607+
        except Exception:  # noqa: BLE001
            dpi = 0
        if dpi <= 0:
            gdi32 = ctypes.windll.gdi32
            hdc = user32.GetDC(0)
            try:
                dpi = int(gdi32.GetDeviceCaps(hdc, 88))   # LOGPIXELSX
            finally:
                user32.ReleaseDC(0, hdc)
        if dpi > 0:
            return round(dpi / 96.0, 3)
    except Exception:  # noqa: BLE001
        pass
    return 1.0


def diagnose_target(target: Optional[FocusTarget] = None, keyword: str = "") -> list:
    """目标窗口体检：返回 [(级别, 文案), ...]，级别 ∈ {"ok", "warn", "info"}。

    调用方按级别加图标即可。只做只读查询，不读取、不修改目标程序的内容。
    """
    items: list = []

    # 1) 先确定要体检的目标：已锁定的目标 > 标题关键字 > 当前前台窗口，
    #    保证即使还没锁定窗口也能给出结论。
    pid = int(getattr(target, "pid", 0) or 0)
    title = str(getattr(target, "title", "") or "")
    proc = str(getattr(target, "proc", "") or "")
    if not (pid or title or proc):
        kw = str(keyword or "").strip()
        picked = None
        if kw:
            for _hwnd, w_title, w_pid in list_windows():
                if kw.lower() in str(w_title).lower():
                    picked = (int(w_pid), str(w_title))
                    break
        if picked is None:
            fg_pid, fg_title, _fg_proc = get_foreground()
            picked = (int(fg_pid), str(fg_title))
        pid, title = picked
        proc = _proc_name(pid)

    if pid or title:
        items.append(("info", f"目标窗口：{title or proc or pid}"
                              f"（pid={pid or '?'}，进程 {proc or '未知'}）"))
    else:
        items.append(("warn", "还没锁定目标窗口，也没读到当前前台窗口 —— "
                              "请先点「① 抓取当前窗口」或「🎯 瞄准镜选窗」锁定目标后重新体检。"))

    # 2) 权限级别 + UIPI：低权限进程发给高权限窗口的输入会被系统静默丢弃
    self_elev = process_elevation(os.getpid())
    tgt_elev = process_elevation(pid)

    if self_elev == "elevated":
        items.append(("ok", "本软件已获得管理员权限，可以向任何权限级别的窗口发送输入。"))
    elif self_elev == "normal":
        items.append(("info", "本软件当前以普通权限运行。"))
    else:
        items.append(("info", "读不到本软件自身的权限级别（不影响使用）。"))

    if tgt_elev == "elevated" and self_elev != "elevated":
        items.append(("warn", "目标程序以【管理员权限】运行，而本软件不是 —— "
                              "Windows 的 UIPI 会静默丢弃本软件发给它的按键/点击。"
                              "浏览器不受影响，所以表现为网页能弹、切到游戏没反应。"
                              "解决：关闭软件后重新双击「启动.bat」，在 UAC 弹窗点「是」以管理员身份启动。"))
    elif tgt_elev == "elevated":
        items.append(("ok", "目标程序以管理员权限运行，但本软件同为管理员，输入不会被拦截。"))
    elif tgt_elev == "normal":
        items.append(("ok", "目标程序以普通权限运行，输入不会被 UIPI 拦截。"))
    else:
        items.append(("info", "读不到目标进程的权限级别（进程可能已退出，或权限不足），"
                              "无法据此判断 UIPI。"))

    # 3) DPI 缩放：鼠标模式按物理像素换算坐标
    scale = system_dpi_scale()
    if abs(scale - 1.0) < 0.01:
        items.append(("ok", "系统 DPI 缩放：100%，鼠标点击坐标与屏幕像素一一对应。"))
    else:
        pct = int(round(scale * 100))
        items.append(("warn", f"系统 DPI 缩放：{pct}%（非 100%）。"
                              "鼠标模式按物理像素换算；若点击位置整体偏移，"
                              "可把 Windows 显示缩放临时调成 100% 后重新做「③ 四点校准」，"
                              "或改用「③ 键盘映射对应」（不依赖屏幕坐标）。"))

    # 4) 注入后端
    try:
        from . import inject

        backend_ok = inject.is_available()
    except Exception:  # noqa: BLE001
        backend_ok = False
    if backend_ok:
        items.append(("ok", "输入注入后端：SendInput（键盘带真实扫描码、鼠标用绝对坐标），"
                            "对 DirectInput / RawInput 类游戏兼容性更好。"))
    else:
        items.append(("info", "输入注入后端：SendInput 不可用，已回退 pyautogui"
                              "（浏览器等 WM_KEYDOWN 类场景不受影响）。"))

    # 5) 当前焦点状态
    fg_pid, fg_title, _fg_proc2 = get_foreground()
    if pid and int(fg_pid or 0) == int(pid):
        items.append(("ok", f"目标窗口此刻就在前台（{fg_title}），可以直接弹奏。"))
    else:
        items.append(("info", f"当前前台是「{fg_title or '未知窗口'}」，不是目标窗口；"
                              "开弹前可点「② 切到该窗口」，或让焦点守卫自动等待/暂停。"))

    return items