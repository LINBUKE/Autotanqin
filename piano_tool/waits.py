"""可中断的等待。

播放器按「目标时刻 - 当前时刻」睡一段再发下一个音。如果直接 `time.sleep(整段)`，
长休止（前奏、间奏、甚至开头 4 秒）里点「停止 / 三连右键暂停」要等这段睡完才响应 ——
用户感知就是「按钮不跟手、卡卡的」。

这里把等待切成 20ms 的小片，每片之前问一次 `stop_check`，停止响应就压到 20ms 以内。
"""

from __future__ import annotations

import time
from typing import Callable, Optional

TICK = 0.02


def sleep_until(deadline: float, stop_check: Optional[Callable[[], bool]] = None,
                tick: float = TICK) -> bool:
    """睡到 `time.perf_counter() >= deadline`。

    返回 True 表示中途被 `stop_check` 叫停（调用方应当结束播放）。
    """
    while True:
        if stop_check is not None and stop_check():
            return True
        left = deadline - time.perf_counter()
        if left <= 0:
            return False
        # 最后不足一片时按剩余时间睡，别多睡
        time.sleep(left if left < tick else tick)


def sleep_for(seconds: float, stop_check: Optional[Callable[[], bool]] = None,
              tick: float = TICK) -> bool:
    """睡指定秒数（同上，可中断）。"""
    if seconds <= 0:
        return stop_check is not None and stop_check()
    return sleep_until(time.perf_counter() + seconds, stop_check, tick)
