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
    "get_foreground",
    "load_target",
    "make_checker",
    "save_target",
    "target_matches",
    "title_matches",
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