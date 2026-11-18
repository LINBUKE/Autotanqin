"""播放中的快捷手势：快速三按鼠标右键 = 暂停/停止。

为什么用「轮询 GetAsyncKeyState」而不是 `SetWindowsHookEx` 鼠标钩子：
钩子必须挂在某个线程的消息循环上，和 Tk 的主循环抢消息、退出时还得小心解钩；
`GetAsyncKeyState` 每 15ms 问一次系统「右键现在按着没」，跨程序都能读到、不占焦点、
停止时只要把线程标志一置就退干净。代价只是极轻的一次系统调用。

判定「三连」：按**按下沿**计数（一直按着不算多次），三次按下都落在同一个
时间窗（默认 700ms）内才算触发；触发后清空计数，避免按住不放连续触发。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

VK_RBUTTON = 0x02


def _rbutton_down() -> bool:
    """右键当前是否按下（全局，不受窗口焦点影响）。"""
    try:
        import ctypes

        return bool(ctypes.windll.user32.GetAsyncKeyState(VK_RBUTTON) & 0x8000)
    except Exception:  # noqa: BLE001  非 Windows
        return False


class TripleRightClickWatcher:
    """后台线程轮询右键，检测到「快速三按」就回调（回调在**工作线程**里执行）。

    调用方（GUI）请把回调里的界面操作丢进自己的队列/after，别在工作线程里直接动 Tk。
    """

    def __init__(self, on_triple: Callable[[], None], count: int = 3,
                 window_s: float = 0.7, poll_s: float = 0.015):
        self._on_triple = on_triple
        self._count = max(2, int(count))
        self._window = max(0.15, float(window_s))
        self._poll = max(0.005, float(poll_s))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.fired = 0          # 触发次数（便于日志/测试）

    # ---- 生命周期 --------------------------------------------------------
    def start(self) -> bool:
        """开始监听（重复调用幂等）。"""
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="triple-rclick",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=0.5)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- 轮询 ------------------------------------------------------------
    def _fire(self) -> None:
        """触发一次回调。异常只记日志——回调挂掉不能让监听线程消失（否则手势失灵还没人知道）。"""
        self.fired += 1
        try:
            self._on_triple()
        except Exception:  # noqa: BLE001
            logger.warning("三连右键回调失败", exc_info=True)

    def _loop(self) -> None:
        presses: List[float] = []
        last_down = False
        while not self._stop.is_set():
            down = _rbutton_down()
            if down and not last_down:                 # 只认按下沿
                now = time.monotonic()
                presses.append(now)
                presses = [t for t in presses if now - t <= self._window]
                if len(presses) >= self._count:
                    presses.clear()
                    self._fire()
            last_down = down
            self._stop.wait(self._poll)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False


def feed_presses(watcher_cls, times: List[float], window_s: float = 0.7,
                 count: int = 3) -> int:
    """纯逻辑版：给一串「按下时刻」，返回会触发几次（供测试用，不碰系统 API）。"""
    presses: List[float] = []
    fired = 0
    for now in times:
        presses.append(now)
        presses = [t for t in presses if now - t <= window_s]
        if len(presses) >= count:
            presses.clear()
            fired += 1
    return fired
