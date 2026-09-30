"""⑤-b 逐键延迟采样：把 21 个键的「点击→发声」延迟测出来，存成样本库。

样本库（key_latency.json）会被播放器读取，用于逐键提前补偿。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .latency_probe import AudioMonitor, measure_key_latency, pick_monitor_device
from .models import Calibration

logger = logging.getLogger("piano_tool.calibrate_latency")


def sample_all_keys(
    calibration: Calibration,
    monitor: Optional[AudioMonitor] = None,
    click_fn: Optional[Callable[[float, float], None]] = None,
    on_each: Optional[Callable[[str, Optional[float]], None]] = None,
    timeout: float = 0.8,
) -> Dict[str, Optional[float]]:
    """逐键测量延迟。返回 {键名: 延迟(秒) 或 None}。

    monitor: AudioMonitor 实例；为 None 时自动构造（默认监听设备）。
    click_fn: 实际点击函数，默认 pyautogui.click(x, y, duration=0, _pause=False)。
    on_each(key, latency): 每测完一个键回调，便于 GUI 刷新进度。
    """
    if monitor is None:
        monitor = AudioMonitor(device=pick_monitor_device())
    if click_fn is None:
        import pyautogui

        def click_fn(x, y):
            pyautogui.click(x, y, duration=0, _pause=False)

    result: Dict[str, Optional[float]] = {}
    for k in calibration.keys:
        try:
            lat = measure_key_latency(click_fn, k.x, k.y, monitor, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            logger.exception("采样键 %s 失败", k.key)
            lat = None
            # 关闭再重建监听，避免单次失败污染后续
            try:
                monitor.close()
                monitor.open()
            except Exception:  # noqa: BLE001
                pass
        result[k.key] = lat
        logger.info("键 %s 延迟=%.1f ms", k.key, (lat * 1000) if lat else float("nan"))
        if on_each is not None:
            on_each(k.key, lat)
    return result


def save_latency_profile(
    latencies: Dict[str, Optional[float]],
    path,
    device_name: str = "",
    notes: str = "",
) -> Path:
    """保存样本库。latencies: {键: 秒 或 None}。"""
    valid = [v for v in latencies.values() if v is not None]
    mean_ms = (sum(valid) / len(valid) * 1000.0) if valid else None
    payload = {
        "sampled_at": datetime.now().isoformat(timespec="seconds"),
        "device": device_name,
        "notes": notes,
        "mean_ms": round(mean_ms, 1) if mean_ms is not None else None,
        "latencies": {k: (round(v * 1000.0, 2) if v is not None else None) for k, v in latencies.items()},
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def load_latency_profile(path) -> Optional[dict]:
    """读取样本库；返回 dict（含 latencies: {键: ms 或 None}）或 None。"""
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取延迟样本库失败：%s", exc)
        return None


def profile_to_lead_seconds(profile: dict) -> Dict[str, float]:
    """把样本库转成 {键: 提前量(秒)}。None 的项不参与（播放时回退到全局提前量）。"""
    out: Dict[str, float] = {}
    for k, v in (profile.get("latencies") or {}).items():
        if v is not None:
            out[k] = float(v) / 1000.0
    return out


def build_profile_from_constant(latency_ms: float, calibration: Calibration, path) -> Path:
    """手动兜底：用一个统一延迟值给所有键建样本库。"""
    latencies = {k.key: latency_ms / 1000.0 for k in calibration.keys}
    return save_latency_profile(
        latencies, path,
        device_name="手动统一延迟",
        notes=f"用户手动指定统一延迟 {latency_ms} ms；未使用音频自动检测",
    )
