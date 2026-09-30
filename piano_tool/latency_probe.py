"""⑤-b 逐键延迟采样：测量每个琴键「点击 → 发声」的端到端延迟，存成样本库。

原理：用 sounddevice 实时捕获音频，在程序点击某个琴键后，检测声音起始
（onset）出现的时刻，latency = 声音起始时刻 − 点击时刻。

这份「逐键样本」用于播放时逐键提前补偿（让声音落在正确节拍上），
比全局填一个固定提前量更准、更稳。

设备选择优先级：
  1. 系统「立体声混音 / Stereo Mix / Loopback / CABLE」等回环/监听输入
     （可直接抓到浏览器模拟器发出的声音，无需麦克风）
  2. 默认输入（麦克风）——把麦克风对着音箱也能用，但受环境噪声影响
若无可用音频设备，会在构造 AudioMonitor 时抛出明确异常，由上层降级到
手动模式。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("piano_tool.latency_probe")

try:
    import sounddevice as sd

    _HAS_SD = True
except Exception:  # noqa: BLE001
    sd = None  # type: ignore
    _HAS_SD = False


def pick_monitor_device() -> Optional[int]:
    """挑选最适合做回环采集的输入设备。返回设备索引或 None（用默认输入）。"""
    if not _HAS_SD:
        return None
    keywords = ("立体声混音", "stereo mix", "loopback", "cable", "monitor")
    try:
        devs = sd.query_devices()
        for i, d in enumerate(devs):
            if int(d.get("max_input_channels", 0)) <= 0:
                continue
            name = (d.get("name") or "").lower()
            if any(k in name for k in keywords):
                return i
    except Exception:  # noqa: BLE001
        pass
    return None


def default_loopback_device() -> Optional[int]:
    """默认「系统声音」回环设备 = 系统默认输出设备（你通常听声音的那种）。

    用于 GUI 自动选中「系统声音(回环)」这一项。
    """
    if not _HAS_SD:
        return None
    try:
        return int(sd.default.device[1])
    except Exception:  # noqa: BLE001
        return None


class AudioMonitor:
    """实时音频能量监测，用于检测声音起始（onset）。"""

    def __init__(
        self,
        device: Optional[int] = None,
        samplerate: Optional[int] = None,
        blocksize: int = 1024,
        loopback: bool = False,
    ):
        if not _HAS_SD:
            raise RuntimeError("未安装 sounddevice，无法进行音频延迟采样（可改用手动模式）")
        self.device = device
        self.loopback = loopback
        # 回环模式：device 实际是「输出设备」，需要捕获它正在播放的声音
        if self.loopback and self.device is None:
            try:
                self.device = sd.default.device[1]
            except Exception:  # noqa: BLE001
                pass
        info = sd.query_devices(device if device is not None else sd.default.device[0])
        self.samplerate = int(samplerate or info["default_samplerate"])
        self.blocksize = blocksize
        self._q: "queue.Queue[Tuple[float, float]]" = queue.Queue(maxsize=4096)
        self._stream = None
        self._stop = threading.Event()

    @property
    def device_name(self) -> str:
        try:
            return str(sd.query_devices(self.device)["name"])
        except Exception:  # noqa: BLE001
            return f"device#{self.device}"

    def _callback(self, indata, frames, time_info, status):
        if self._stop.is_set():
            return
        try:
            mono = indata[:, 0] if indata.shape[1] > 1 else indata[:, 0]
            rms = float(np.sqrt(np.mean(np.square(mono.astype(np.float32)))))
            self._q.put((time.perf_counter(), rms))
        except Exception:  # noqa: BLE001
            pass

    def open(self):
        self._stop.clear()
        kwargs = dict(
            device=self.device,
            samplerate=self.samplerate,
            blocksize=self.blocksize,
            channels=1,
            callback=self._callback,
        )
        # WASAPI 回环：捕获「输出设备正在播放的声音」= 录屏软件说的系统声音
        if self.loopback:
            try:
                kwargs["extra_settings"] = sd.WasapiSettings(include_loopback=True)
            except Exception:  # noqa: BLE001
                pass
        self._stream = sd.InputStream(**kwargs)
        self._stream.start()

    def close(self):
        self._stop.set()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
        self._stream = None
        # 清空队列
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except Exception:  # noqa: BLE001
                break

    def _drain_old(self):
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except Exception:  # noqa: BLE001
                break

    def baseline(self, blocks: int = 12) -> Tuple[float, float]:
        """采集静默基线：返回 (均值, 标准差)。"""
        self._drain_old()
        vals: List[float] = []
        deadline = time.perf_counter() + 3.0
        while len(vals) < blocks and time.perf_counter() < deadline:
            try:
                _, rms = self._q.get(timeout=0.2)
                vals.append(rms)
            except Exception:  # noqa: BLE001
                break
        if not vals:
            return 0.0, 0.0
        arr = np.array(vals, dtype=np.float64)
        return float(arr.mean()), float(arr.std())

    def wait_onset(
        self,
        after: float,
        timeout: float = 0.8,
        baseline_mean: float = 0.0,
        baseline_std: float = 0.0,
    ) -> Optional[float]:
        """从 after（perf_counter 时刻）之后等待声音 onset，返回 onset 时刻；超时返回 None。"""
        floor = max(baseline_mean * 3.0, baseline_mean + 5.0 * baseline_std, 1e-4)
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            try:
                t, rms = self._q.get(timeout=0.05)
            except Exception:  # noqa: BLE001
                continue
            if t < after:
                continue
            if rms >= floor:
                return t
        return None


def list_capture_devices() -> List[dict]:
    """列出可用于「采到模拟器声音」的捕获设备，含两类：

    - 真实输入（麦克风 / 立体声混音 / CABLE 等）：loopback=False
    - 系统声音回环（把某个输出设备正在播放的声音抓回来）：loopback=True
      这正是录屏软件里说的「系统声音」。Windows 上没有独立的「系统声音」输入
      设备，它是通过 WASAPI 回环捕获输出设备实现的，所以这里显式列出来。
    """
    if not _HAS_SD:
        return []
    try:
        devs = sd.query_devices()
    except Exception:  # noqa: BLE001
        return []
    out: List[dict] = []
    for i, d in enumerate(devs):
        if int(d.get("max_input_channels", 0)) > 0:
            out.append({
                "index": i,
                "name": str(d.get("name", "")),
                "samplerate": int(d.get("default_samplerate", 0)),
                "loopback": False,
            })
    # 输出设备 → 系统声音回环选项（抓「正在播放的声音」）
    for i, d in enumerate(devs):
        if int(d.get("max_output_channels", 0)) > 0:
            out.append({
                "index": i,
                "name": f"系统声音(回环) · {d.get('name')}",
                "samplerate": int(d.get("default_samplerate", 0)),
                "loopback": True,
            })
    return out


def test_audio_level(device: Optional[int] = None, duration: float = 2.0,
                     loopback: bool = False) -> Tuple[float, Optional[str]]:
    """检测音频电平：打开监听设备 duration 秒，返回最大 RMS 和设备名。

    用于让用户确认声音是否真的进入了程序（例如浏览器输出是否路由到
    回环设备，或麦克风是否收音）。
    loopback=True 时捕获「系统声音（输出设备正在播放的声音）」。
    """
    if not _HAS_SD:
        return 0.0, None
    monitor = AudioMonitor(device=device, loopback=loopback)
    monitor.open()
    time.sleep(0.1)
    monitor._drain_old()
    max_rms = 0.0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < duration:
        try:
            _, rms = monitor._q.get(timeout=0.05)
            if rms > max_rms:
                max_rms = rms
        except Exception:  # noqa: BLE001
            continue
    monitor.close()
    return max_rms, monitor.device_name


def measure_key_latency(
    click_fn: Callable[[float, float], None],
    x: float,
    y: float,
    monitor: AudioMonitor,
    timeout: float = 0.8,
    post_pause: float = 0.25,
) -> Optional[float]:
    """点击 (x, y) 处的琴键，返回「点击→发声」延迟（秒）；失败返回 None。

    click_fn: 实际执行点击的函数（默认 pyautogui.click(x, y, duration=0, _pause=False)）。
    """
    monitor.open()
    try:
        mean, std = monitor.baseline(blocks=12)
        # 清掉基线期间可能残留的块
        monitor._drain_old()
        t_click = time.perf_counter()
        click_fn(x, y)
        onset = monitor.wait_onset(after=t_click, timeout=timeout, baseline_mean=mean, baseline_std=std)
        if onset is None:
            logger.warning("键 (%s, %s) 未检测到发声，可能静音/设备未捕获到", x, y)
            return None
        return max(0.0, onset - t_click)
    finally:
        time.sleep(post_pause)  # 让余音结束，避免影响下一键基线
        monitor.close()
