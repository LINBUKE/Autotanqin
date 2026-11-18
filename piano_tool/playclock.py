"""播放时钟：给浏览器编辑器同步播放头用。

界面上的播放头不能靠「逐音回报」——密集连音时每秒几十次回调，还得跨线程。
这里只记一个起点，位置按 **已过时间 × 倍速** 实时推算：

    position = start_time + (now - start_wall) * speed

停止/暂停时把当时的位置**冻结**进 `position`，之后 `playing=False` 但位置不再变 ——
网页那边就能「停在最后那一刻」，而不是跳回开头。

`now` 可注入（测试用假时钟），线程安全（后台播放线程写、HTTP 线程读）。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict


class PlayClock:
    """一个极小的播放进度钟。"""

    def __init__(self, now: Callable[[], float] = time.time):
        self._now = now
        self._lock = threading.Lock()
        self._playing = False
        self._start_wall = 0.0
        self._start_time = 0.0
        self._speed = 1.0
        self._total = 0.0
        self._position = 0.0

    # ---- 写入 ------------------------------------------------------------
    def start(self, speed: float = 1.0, total: float = 0.0,
              start_time: float = 0.0) -> None:
        """开播打点（倒计时结束后才该调用）。"""
        with self._lock:
            # 注意别写成 `speed or 1.0`：0 是 falsy，会把「暂停播放」误当成 1 倍速
            raw_speed = 1.0 if speed is None else float(speed)
            self._speed = max(0.05, raw_speed)
            self._total = max(0.0, float(total or 0.0))
            self._start_time = max(0.0, float(start_time or 0.0))
            self._start_wall = float(self._now())
            self._position = self._start_time
            self._playing = True

    def freeze(self) -> float:
        """停止/暂停：把当前位置定格，返回定格值（重复调用不会继续前进）。"""
        with self._lock:
            if self._playing:
                self._position = self._calc_locked()
                self._playing = False
            return round(self._position, 3)

    def reset(self) -> None:
        """回到「没播过」的状态。"""
        with self._lock:
            self._playing = False
            self._position = 0.0
            self._start_wall = 0.0
            self._start_time = 0.0
            self._total = 0.0

    # ---- 读取 ------------------------------------------------------------
    @property
    def playing(self) -> bool:
        with self._lock:
            return self._playing

    def position(self) -> float:
        with self._lock:
            return round(self._calc_locked(), 3)

    def snapshot(self) -> Dict[str, float]:
        """HTTP 端点直接返回这个。"""
        with self._lock:
            return {
                "playing": self._playing,
                "position": round(self._calc_locked(), 3),
                "total": round(self._total, 3),
                "speed": round(self._speed, 3),
            }

    # ---- 内部 ------------------------------------------------------------
    def _calc_locked(self) -> float:
        """调用方必须已持锁。

        未播放时返回**冻结位置**（不是 0）—— 这正是「暂停后网页播放头停在原地」
        的关键，返回 0 会让它跳回开头。
        """
        if not self._playing:
            return max(0.0, self._position)
        pos = self._start_time
        elapsed = max(0.0, float(self._now()) - self._start_wall)
        pos += elapsed * self._speed
        if self._total > 0:
            pos = min(pos, self._total)
        return max(0.0, pos)
