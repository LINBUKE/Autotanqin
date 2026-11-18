"""防封随机化（可选）：鼠标点击位置 + 键盘按键时刻。

纯自动化如果每次都分毫不差，长期重复容易被风控。这里做「人不会那么准」的
一点点扰动，但**不改音高、不改谱子**：

- 鼠标模式：每次点击在琴键中心附近随机偏移几像素（可调 0~10px），
  down/up 用同一个偏移，不会点到隔壁键；
- 键盘模式：从**开头 / 中段（高潮附近）/ 末尾**三段里均匀抽 N 个音，
  只把这些音的按键时刻抖动 ±几毫秒（可调 0~80ms），其余音完全不变；
- 随机种子：留空 = 每次播放自动换一个；填数字 = 每次相同（方便对比/排查）。
"""
from __future__ import annotations

import random
import secrets
from typing import Dict, List, Optional, Sequence, Tuple

MAX_SEED = 2 ** 31 - 1
MAX_MOUSE_PX = 10
MAX_KEY_COUNT = 200
MAX_KEY_JITTER_MS = 80


def new_seed() -> int:
    """系统随机种子（不依赖时间，避免连续两次播放撞种子）。"""
    return secrets.randbelow(MAX_SEED)


def resolve_seed(text) -> int:
    """界面上的种子文本 → 整数。留空/非法值都返回新的随机种子。"""
    s = str(text or "").strip()
    if not s:
        return new_seed()
    try:
        return max(0, int(float(s))) % MAX_SEED
    except (TypeError, ValueError):
        return new_seed()


def clamp_mouse_px(value) -> int:
    try:
        return max(0, min(MAX_MOUSE_PX, int(float(value))))
    except (TypeError, ValueError):
        return 0


def clamp_key_count(value) -> int:
    try:
        return max(0, min(MAX_KEY_COUNT, int(float(value))))
    except (TypeError, ValueError):
        return 0


def clamp_jitter_ms(value) -> int:
    try:
        return max(0, min(MAX_KEY_JITTER_MS, int(float(value))))
    except (TypeError, ValueError):
        return 0


def pick_indices(events: Sequence, count: int, seed: int) -> List[int]:
    """按时间分三段（开头 / 中段 / 末尾），轮询均匀抽 count 个下标。

    返回的是 events 里的原始下标（不排序），方便调用方按 id / 下标取用。
    三个段都有人时，保证每段至少抽到一个（count >= 3 时）。
    """
    n = len(events)
    count = max(0, min(int(count), n))
    if count == 0:
        return []

    order = sorted(range(n), key=lambda i: (float(getattr(events[i], "time", 0.0) or 0.0), i))
    if n < 3:
        rng = random.Random(seed)
        return rng.sample(order, count)

    cuts = (0, max(1, n // 3), max(2, (2 * n) // 3), n)
    pools: List[List[int]] = []
    for a, b in zip(cuts, cuts[1:]):
        pool = order[a:b]
        if pool:
            pools.append(pool)
    rng = random.Random(seed)
    for pool in pools:
        rng.shuffle(pool)

    picked: List[int] = []
    while len(picked) < count and any(pools):
        for pool in pools:
            if pool and len(picked) < count:
                picked.append(pool.pop())
    return picked


def keyboard_time_offsets(events: Sequence, count: int, jitter_ms: int,
                          seed: int) -> Dict[int, float]:
    """给抽中的音生成时刻偏移（秒），键是 events 的原始下标。"""
    jitter_ms = max(0, int(jitter_ms))
    if jitter_ms <= 0:
        return {}
    picked = pick_indices(events, count, seed)
    rng = random.Random((seed ^ 0x5DEECE66D) & MAX_SEED)
    return {i: rng.uniform(-jitter_ms, jitter_ms) / 1000.0 for i in picked}


def click_offsets(events: Sequence, max_px: int, seed: int) -> Dict[int, Tuple[int, int]]:
    """给每个音生成鼠标偏移（像素），键是 event 的 id()。"""
    max_px = max(0, int(max_px))
    if max_px <= 0:
        return {}
    rng = random.Random((seed ^ 0xA5A5A5A5) & MAX_SEED)
    return {
        id(e): (rng.randint(-max_px, max_px), rng.randint(-max_px, max_px))
        for e in events
    }


def describe(seed: int, mouse_px: int, key_count: int, jitter_ms: int) -> str:
    """一行日志文案（0 值不写，避免刷屏）。"""
    parts = [f"种子 {seed}"]
    if mouse_px:
        parts.append(f"鼠标偏移 ±{mouse_px}px")
    if key_count and jitter_ms:
        parts.append(f"键盘随机 {key_count} 个音 ±{jitter_ms}ms（开头/中段/末尾均匀）")
    return "🎲 随机化：" + "，".join(parts)
