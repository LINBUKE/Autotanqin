"""底层输入注入（Windows SendInput 封装）。

为什么需要它
------------
pyautogui 在 Windows 上走的是老接口：

    * 键盘 keybd_event(vkCode, 0, ...)  —— **扫描码固定传 0**
    * 鼠标 SetCursorPos + mouse_event

浏览器只认 WM_KEYDOWN / WM_KEYUP，所以老接口在浏览器里一切正常；
但一部分游戏走 DirectInput / RawInput，会忽略「扫描码为 0」的按键事件，
于是同一套注入出现「浏览器能弹、切到游戏里一声不吭」。

本模块改用 SendInput：

    * 键盘：先用 MapVirtualKeyEx 取该键的**真实扫描码**，再带 KEYEVENTF_SCANCODE
      发出去，DirectInput / RawInput 类游戏也能识别。
    * 鼠标：用 MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK，在虚拟桌面（多
      显示器）坐标系里把目标点归一化到 0~65535，一次调用里完成「移动 + 按下/抬起」。

依然是 Windows 标准 API（SendInput），**不读游戏内存、不改游戏文件、不注入别的进程**。

安全兜底
--------
任何一步失败（非 Windows / ctypes 调用出错 / 拿不到扫描码）都返回 False，由调用方
回退到 pyautogui —— 保证浏览器场景和旧机器上的行为完全不变。
鼠标的「移到屏幕四角急停」FAILSAFE 在本模块内自行实现（行为与 pyautogui 一致），
不会因为绕开 pyautogui 而丢掉急停能力。
"""
from __future__ import annotations

import ctypes
import logging
import sys
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("piano_tool.inject")

__all__ = [
    "click",
    "cursor_position",
    "ensure_dpi_aware",
    "is_available",
    "mouse_down",
    "mouse_up",
    "send_key",
]

# ---- SendInput 常量 ---------------------------------------------------------
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

MAPVK_VK_TO_VSC = 0x0000

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# 需要带 KEYEVENTF_EXTENDEDKEY 的虚拟键（方向键 / 小键盘 / 右 Ctrl 等）。
# 本工具实际只发字母与 Shift，列出来只是让映射更通用。
_EXTENDED_VKS = frozenset({
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,  # PgUp/PgDn/End/Home/↑↓←→
    0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0x5F, 0x6F, 0xA3, 0xA5,
})

# 键名 -> 虚拟键码（字母/数字走字符分支，这里只放命名键）
_VK_NAMES = {
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
}


# ---- 缓存：进程生命周期内不会变的东西，查一次就记住 ----------------------
# 密集连音时每个音符都会走一遍查询，这几张表把 3 次内核调用（GetKeyboardLayout /
# MapVirtualKeyEx 每次都要做）压到只在首次各做一次，避免把时间花在重复查询上。
_VK_CACHE: Dict[str, Tuple[int, bool]] = {}
_SCAN_CACHE: Dict[int, int] = {}
_HKL = None
_CORNERS: Optional[set] = None


def ensure_dpi_aware() -> None:
    """让本进程按**物理像素**读屏幕尺寸（与 pyautogui 导入时的行为一致）。

    SendInput 的绝对坐标以物理像素为基准。进程若「DPI 不感知」，GetSystemMetrics
    会返回被系统按缩放折算过的逻辑尺寸（例如 2560×1600 在 250% 缩放下变成
    1707×1067），归一化换算会整体偏小 → 鼠标点不到目标。这里补一层，保证无论
    pyautogui 是否已被导入、坐标换算都以物理像素为准。
    失败（Windows 版本不支持 / 权限已设定过）不影响任何逻辑，仅保持现状。
    """
    try:
        _user32.SetProcessDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
        _user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        _user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


# ---- ctypes 结构体 + DLL 句柄（仅 Windows 上初始化，失败即降级）-------------
_READY = False
_user32 = None
_INPUT = None
_MOUSEINPUT = None
_KEYBDINPUT = None
_POINT = None

if sys.platform == "win32":
    try:
        from ctypes import wintypes

        _ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

        class _MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", _ULONG_PTR),
            ]

        class _KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", _ULONG_PTR),
            ]

        class _HARDWAREINPUT(ctypes.Structure):
            _fields_ = [
                ("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD),
            ]

        class _INPUTUNION(ctypes.Union):
            _fields_ = [
                ("mi", _MOUSEINPUT),
                ("ki", _KEYBDINPUT),
                ("hi", _HARDWAREINPUT),
            ]

        class _INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _user32.SendInput.argtypes = (
            wintypes.UINT,
            ctypes.POINTER(_INPUT),
            ctypes.c_int,
        )
        _user32.SendInput.restype = wintypes.UINT
        _user32.MapVirtualKeyExW.argtypes = (wintypes.UINT, wintypes.UINT, wintypes.HKL)
        _user32.MapVirtualKeyExW.restype = wintypes.UINT
        _user32.GetKeyboardLayout.argtypes = (wintypes.DWORD,)
        _user32.GetKeyboardLayout.restype = wintypes.HKL
        _user32.VkKeyScanW.argtypes = (ctypes.c_wchar,)
        _user32.VkKeyScanW.restype = ctypes.c_short
        _user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
        _user32.GetSystemMetrics.restype = ctypes.c_int
        _user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
        _user32.GetCursorPos.restype = wintypes.BOOL

        _MOUSEINPUT, _KEYBDINPUT, _INPUT, _POINT = _MOUSEINPUT, _KEYBDINPUT, _INPUT, wintypes.POINT
        _READY = True
        ensure_dpi_aware()
    except Exception:  # noqa: BLE001  非 Windows / 结构体定义失败都不该影响主流程
        _READY = False
        _user32 = None
        logger.debug("SendInput 后端初始化失败，将回退 pyautogui", exc_info=True)


def is_available() -> bool:
    """SendInput 后端是否可用（不可用时调用方一律回退 pyautogui）。"""
    return _READY


# ---------------------------------------------------------------------------
# 键名 -> 虚拟键 + 是否需要扩展键标志（结果缓存，密集连音时不重复查）
# ---------------------------------------------------------------------------
def _lookup_vk(k: str) -> Tuple[int, bool]:
    if k in _VK_NAMES:
        vk = int(_VK_NAMES[k])
        return vk, vk in _EXTENDED_VKS
    if len(k) == 1:
        ch = k.upper()
        if "A" <= ch <= "Z" or "0" <= ch <= "9":
            return ord(ch), False
        try:
            res = int(_user32.VkKeyScanW(k))
            vk = res & 0xFF
            if vk:
                return vk, vk in _EXTENDED_VKS
        except Exception:  # noqa: BLE001
            pass
    return 0, False


def _vk_and_extended(key) -> Tuple[int, bool]:
    """键名 -> (虚拟键码, 是否需要 EXTENDEDKEY)；识别不了返回 (0, False)。

    首次查询后缓存：键盘布局在一次进程生命周期里不会变，重复查是白费时间。
    """
    k = str(key).strip().lower()
    if not k:
        return 0, False
    cached = _VK_CACHE.get(k)
    if cached is not None:
        return cached
    result = _lookup_vk(k)
    _VK_CACHE[k] = result
    return result


def _keyboard_layout():
    """当前线程的键盘布局 HKL（只在首次查一次，之后复用）。"""
    global _HKL
    if _HKL is None:
        try:
            _HKL = _user32.GetKeyboardLayout(0)
        except Exception:  # noqa: BLE001
            return 0
    return _HKL


def _scan_for(vk: int) -> int:
    """虚拟键码 -> 键盘扫描码（结果缓存，避免每次重查 MapVirtualKeyEx）。"""
    scan = _SCAN_CACHE.get(vk)
    if scan is not None:
        return scan
    try:
        scan = int(_user32.MapVirtualKeyExW(vk, MAPVK_VK_TO_VSC, _keyboard_layout())) & 0xFF
    except Exception:  # noqa: BLE001
        return 0
    if scan:
        _SCAN_CACHE[vk] = scan
    return scan


def _make_key_input(key, down: bool):
    """构造一个「带真实扫描码」的键盘 INPUT；键不认识 / 拿不到扫描码返回 None。"""
    vk, extended = _vk_and_extended(key)
    if not vk:
        return None
    scan = _scan_for(vk)
    if not scan:
        return None
    flags = KEYEVENTF_SCANCODE
    if not down:
        flags |= KEYEVENTF_KEYUP
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp = _INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = _KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def _send(inputs: List) -> bool:
    """把若干 INPUT 结构一次发给 SendInput；全部投递成功才返回 True。"""
    n = len(inputs)
    if not n:
        return False
    arr = (_INPUT * n)(*inputs)
    sent = int(_user32.SendInput(n, arr, ctypes.sizeof(_INPUT)))
    return sent == n


# ---------------------------------------------------------------------------
# 键盘：对外主入口
# ---------------------------------------------------------------------------
def send_key(key, down: bool, upper: bool = False) -> bool:
    """按/抬一个键；返回 True 表示已用 SendInput 发出，False 表示需回退 pyautogui。

    upper=True 时先按 Shift（down 顺序：Shift↓ → 键↓；up 顺序：键↑ → Shift↑），
    让浏览器/游戏收到大写字母，语义与 keyboard_play 里的老实现一致。
    Shift 与主键**合并成一次 SendInput** 投递，减少一半系统调用与调度抖动。
    """
    if not _READY:
        return False
    ch = str(key).strip().lower()
    if not ch:
        return False
    if upper and down:
        names = [("shift", True), (ch, True)]
    elif upper:
        names = [(ch, False), ("shift", False)]
    else:
        names = [(ch, down)]
    inputs = []
    for name, is_down in names:
        inp = _make_key_input(name, is_down)
        if inp is None:
            return False
        inputs.append(inp)
    try:
        return _send(inputs)
    except Exception:  # noqa: BLE001
        logger.debug("send_key 失败，回退 pyautogui", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# 鼠标：绝对坐标（虚拟桌面）+ 四角急停
# ---------------------------------------------------------------------------
def cursor_position() -> Tuple[int, int]:
    """当前鼠标屏幕坐标；拿不到返回 (0, 0)。"""
    if not _READY:
        return 0, 0
    try:
        pt = _POINT()
        if _user32.GetCursorPos(ctypes.byref(pt)):
            return int(pt.x), int(pt.y)
    except Exception:  # noqa: BLE001
        pass
    return 0, 0


def _virtual_screen() -> Tuple[int, int, int, int]:
    """所有显示器联合虚拟屏幕的 (x, y, 宽, 高)。"""
    x = int(_user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    y = int(_user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    w = int(_user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    h = int(_user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    if w <= 0 or h <= 0:  # 老系统拿不到虚拟屏时退回主屏
        x, y = 0, 0
        w = int(_user32.GetSystemMetrics(0))
        h = int(_user32.GetSystemMetrics(1))
    return x, y, max(w, 1), max(h, 1)


def _to_absolute(x, y) -> Tuple[int, int]:
    """屏幕像素坐标 -> SendInput 虚拟桌面归一化坐标（0~65535）。"""
    vx, vy, vw, vh = _virtual_screen()
    ax = int(round((float(x) - vx) * 65535.0 / max(vw - 1, 1)))
    ay = int(round((float(y) - vy) * 65535.0 / max(vh - 1, 1)))
    return max(0, min(65535, ax)), max(0, min(65535, ay))


def _mouse_event(x, y, extra_flags: int) -> bool:
    """一次 SendInput 完成「移动到 (x, y) + 额外按钮动作」。"""
    ax, ay = _to_absolute(x, y)
    flags = (
        MOUSEEVENTF_MOVE
        | MOUSEEVENTF_ABSOLUTE
        | MOUSEEVENTF_VIRTUALDESK
        | int(extra_flags)
    )
    inp = _INPUT(type=INPUT_MOUSE)
    inp.u.mi = _MOUSEINPUT(dx=ax, dy=ay, mouseData=0, dwFlags=flags, time=0, dwExtraInfo=0)
    return _send([inp])


def _screen_corners() -> set:
    """屏幕四角的坐标集合（只在首次算一次；分辨率不会中途变）。"""
    global _CORNERS
    if _CORNERS is None:
        try:
            import pyautogui

            w, h = pyautogui.size()
        except Exception:  # noqa: BLE001
            w, h = 0, 0
        corners = {(0, 0)}
        if w and h:
            corners |= {(w - 1, 0), (0, h - 1), (w - 1, h - 1)}
        _CORNERS = corners
    return _CORNERS


def _failsafe_check() -> None:
    """屏幕四角急停：与 pyautogui.FAILSAFE 行为一致，命中就抛 FailSafeException。

    绕开 pyautogui 后必须自己补上这一层，否则「鼠标甩到角落紧急停止」会失效。
    异常类型沿用 pyautogui.FailSafeException，播放器原有的急停分支无需改动。
    """
    try:
        import pyautogui
    except Exception:  # noqa: BLE001  没装 pyautogui 就没这层急停
        return
    if not getattr(pyautogui, "FAILSAFE", True):
        return
    if cursor_position() in _screen_corners():
        raise pyautogui.FailSafeException(
            "Fail-safe triggered from mouse moving to a corner of the screen."
        )


def mouse_down(x, y, failsafe: bool = True) -> bool:
    """按下左键（先把指针移到 x, y）。返回 False 表示需回退 pyautogui。

    failsafe=False 供播放器在急停收尾时「抬键」用：那一刻指针恰好在角落，
    若仍做四角检查会再次抛异常、导致收尾中断。
    """
    if not _READY:
        return False
    if failsafe:
        _failsafe_check()   # FailSafeException 必须原样上抛
    try:
        return _mouse_event(x, y, MOUSEEVENTF_LEFTDOWN)
    except Exception:  # noqa: BLE001
        logger.debug("mouse_down 失败，回退 pyautogui", exc_info=True)
        return False


def mouse_up(x, y, failsafe: bool = True) -> bool:
    """抬起左键（先把指针移到 x, y）。"""
    if not _READY:
        return False
    if failsafe:
        _failsafe_check()
    try:
        return _mouse_event(x, y, MOUSEEVENTF_LEFTUP)
    except Exception:  # noqa: BLE001
        logger.debug("mouse_up 失败，回退 pyautogui", exc_info=True)
        return False


def click(x, y) -> bool:
    """瞬时点击：移动到 (x, y) → 按下 → 抬起。"""
    if not _READY:
        return False
    _failsafe_check()
    try:
        if not _mouse_event(x, y, MOUSEEVENTF_LEFTDOWN):
            return False
        return _mouse_event(x, y, MOUSEEVENTF_LEFTUP)
    except Exception:  # noqa: BLE001
        logger.debug("click 失败，回退 pyautogui", exc_info=True)
        return False
