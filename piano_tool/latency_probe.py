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
import re
import threading
import time
from collections import deque
from typing import Callable, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("piano_tool.latency_probe")

try:
    import sounddevice as sd

    _HAS_SD = True
except Exception:  # noqa: BLE001
    sd = None  # type: ignore
    _HAS_SD = False


# ---------------------------------------------------------------------------
# 共享 PyAudio 实例
#
# WASAPI 上频繁 `Pa_Initialize / Pa_Terminate` 会抛
# `OSError: [Errno -9999] Unanticipated host error`（尤其 start_stream 那一步）。
# 老实现每个键都 `pyaudio.PyAudio()` + `terminate()`：21 个键 = 42 次重新枚举
# 设备，几乎必然在某个键上炸掉，而且之后**每个键都跟着失败**（用户看到的就是
# 一屏「延迟=nan」）。这里全局只初始化一次，平时只开关流。
# ---------------------------------------------------------------------------
_PA_LOCK = threading.Lock()
_PA_SHARED = None


def _shared_pyaudio():
    """取全局 PyAudio 实例（第一次调用时创建）。"""
    global _PA_SHARED
    import pyaudiowpatch as pyaudio

    with _PA_LOCK:
        if _PA_SHARED is None:
            _PA_SHARED = pyaudio.PyAudio()
        return _PA_SHARED


def _reset_pyaudio() -> None:
    """-9999 之后重建实例：PortAudio 内部状态脏了，只能整体重来。"""
    global _PA_SHARED
    with _PA_LOCK:
        pa, _PA_SHARED = _PA_SHARED, None
    if pa is not None:
        try:
            pa.terminate()
        except Exception:  # noqa: BLE001
            pass
        logger.info("已重置 PyAudio 实例（上一个流开失败）")


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


class AudioMonitor:
    """实时音频能量监测，用于检测声音起始（onset）。

    `collect_audio=True` 时额外把**最近几秒的原始波形**留在环形缓冲里，
    采样结束后可以按时间区间截一段（`grab`）——「模拟音」用的就是这部分：
    延迟采样顺便把每个键真实发出的声音录下来，浏览器拿它当音色。
    """

    def __init__(
        self,
        device: Optional[int] = None,
        samplerate: Optional[int] = None,
        blocksize: int = 1024,
        loopback: bool = False,
        collect_audio: bool = False,
        keep_seconds: float = 4.0,
    ):
        if not _HAS_SD:
            raise RuntimeError("未安装 sounddevice，无法进行音频延迟采样（可改用手动模式）")
        self.device = device
        self.loopback = loopback
        self.blocksize = blocksize
        # 回环用 PyAudio 的设备索引（和 sounddevice 是两套编号），别拿去 query_devices
        resolved = self._resolve_device() if not loopback else self.device
        self.device = resolved
        if loopback:
            # 回环统一 48k：WASAPI 共享模式会自动重采样，
            # 有些蓝牙设备默认只有 8k，直接用会开不起来
            default_sr = 48000
        else:
            try:
                info = sd.query_devices(resolved if resolved is not None else 0)
                default_sr = int(info.get("default_samplerate", 48000) or 48000)
            except Exception:  # noqa: BLE001  拿不到就用 48k，后面开流时会再校验
                default_sr = 48000
        self.samplerate = int(samplerate or default_sr)
        self._q: "queue.Queue[Tuple[float, float]]" = queue.Queue(maxsize=4096)
        self._stream = None
        self._pa = None             # 共享的 PyAudio 实例（系统声音回环）
        self._pa_stream = None
        self._pa_buffer = 0         # 实际用上的 frames_per_buffer（诊断用）
        self._pa_continue = 0
        self._loop_source = ""      # 实际用的回环后端描述（日志/诊断用）
        self._stop = threading.Event()
        # 最近一次 wait_onset 的诊断值：失败时能区分「完全没声音」和「有声音但没抓到」
        self.last_peak = 0.0
        self.last_floor = 0.0
        # 波形环形缓冲：[(块起始时间, mono float32 数组)]
        self._collect = bool(collect_audio)
        self._audio: "deque" = deque(maxlen=max(8, int(keep_seconds * self.samplerate / max(1, blocksize)) + 4))

    @property
    def is_open(self) -> bool:
        """当前是否已经有一条活着的采集流（采样时用一条流跑完所有键）。"""
        if self.loopback:
            st = getattr(self, "_pa_stream", None)
            if st is None:
                return False
            try:
                return bool(st.is_active())
            except Exception:  # noqa: BLE001
                return True
        st = self._stream
        if st is None:
            return False
        try:
            return bool(st.active)
        except Exception:  # noqa: BLE001
            return True

    @property
    def device_name(self) -> str:
        if self.loopback:
            return self._loop_source or "系统声音(回环)"
        try:
            return str(sd.query_devices(self.device)["name"])
        except Exception:  # noqa: BLE001
            return f"device#{self.device}"

    def _callback(self, indata, frames, time_info, status):
        if self._stop.is_set():
            return
        try:
            mono = indata[:, 0] if indata.shape[1] > 1 else indata[:, 0]
            mono = mono.astype(np.float32)
            now = time.perf_counter()
            rms = float(np.sqrt(np.mean(np.square(mono))))
            if self._collect:
                # 存**块起点**，不是回调时刻：回调是在这一块录满之后才被调用的，
                # 直接存 now 会让 grab() 的每一段整体晚一个块长（1024/48000 ≈ 21ms），
                # 逐键延迟就系统性偏大 21ms。
                self._audio.append((now - len(mono) / float(self.samplerate), mono.copy()))
            self._q.put((now, rms))
        except Exception:  # noqa: BLE001
            pass

    # ---- 波形截取（collect_audio=True 才有数据）---------------------------
    def grab(self, start: float, end: float) -> Optional["np.ndarray"]:
        """截取 [start, end) 这段 perf_counter 时间的波形；没数据返回 None。"""
        if not self._collect or end <= start:
            return None
        parts: List["np.ndarray"] = []
        for t_block, clip in list(self._audio):
            block_end = t_block + len(clip) / float(self.samplerate)
            if block_end <= start or t_block >= end:
                continue
            a = int(max(0.0, start - t_block) * self.samplerate)
            b = int(min(len(clip), (end - t_block) * self.samplerate))
            if b > a:
                parts.append(clip[a:b])
        if not parts:
            return None
        return np.concatenate(parts)

    def grab_clip(self, onset: float, pre: float = 0.05,
                  post: float = 0.85) -> Optional["np.ndarray"]:
        """截取「发声前后」一段（默认 50ms 前 + 1150ms 后），当作这个键的音色。

        `post` 必须够长（钢琴余音有一秒多），而且**调用前必须等够 post 秒**：
        `wait_onset()` 一检测到声音就返回，那时缓冲里只有刚发生的那几十毫秒，
        立刻 grab 只能拿到一段 10~20ms 的「碎片」（浏览器里就是「咔」一下）。
        """
        return self.grab(onset - max(0.0, pre), onset + max(0.0, post))

    def _resolve_device(self) -> Optional[int]:
        """没指定设备时挑一个能用的：优先默认输出（回环）／第一个有输入的设备。"""
        if self.device is not None:
            return int(self.device)
        if self.loopback:
            # PyAudio 的回环索引和 sounddevice 的索引是两套编号，不能互猜；
            # 返回 None，由 _pick_pa_loopback() 按「默认输出 → 第一条」挑。
            return None
        try:
            inp = sd.default.device[0]
            if inp is not None and int(inp) >= 0:
                return int(inp)
        except Exception:  # noqa: BLE001
            pass
        # 默认设备不可用（常见于「默认输入 = -1」的机器）：退到第一个可用设备
        try:
            want_out = self.loopback
            for i, d in enumerate(sd.query_devices()):
                ch = int(d.get("max_output_channels" if want_out else "max_input_channels", 0) or 0)
                if ch > 0:
                    return i
        except Exception:  # noqa: BLE001
            pass
        return None

    def _assert_loopback_supported(self) -> None:
        """系统声音回环只走 pyaudiowpatch（PyAudio 的 WASAPI loopback 分支）。

        为什么只用它：
        - **sounddevice 做不到**：`WasapiSettings` 从来没有 `include_loopback` 参数
          （0.5.x 签名只有 exclusive/auto_convert/explicit_sample_format）。以前写
          `sd.WasapiSettings(include_loopback=True)` 会被 except 吞掉、悄悄退回默认
          输入 —— 表现就是「选了系统声音，录到的却是麦克风」。
        - **soundcard 在 Windows 上不稳**：常抛 0x800401f0 / -9999，而且只认一块
          扬声器，选别的会静默退回默认那块（「选了 A 实际采的是 B」）。
        pyaudiowpatch 直接抓 WASAPI 回环，**不需要 VB-Cable / 立体声混音**，
        蓝牙耳机、HDMI/NVIDIA 音频、主板声卡都能采。
        """
        try:
            import pyaudiowpatch  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "录制系统声音需要 pyaudiowpatch：\n"
                "  .venv\\Scripts\\python.exe -m pip install pyaudiowpatch\n"
                "它不需要装任何虚拟声卡驱动。装不上就在下拉里改用「🎤 立体声混音」，"
                "或改用手动统一延迟。") from exc

    def _open_loopback_stream(self) -> None:
        """打开系统声音回环（pyaudiowpatch）。

        **绝不静默退回默认输入**（那就是麦克风）—— 找不到就报错并列出能用哪些。
        """
        self._open_loopback_pyaudio()

    @staticmethod
    def _norm(name: str) -> str:
        """设备名规范化：去 [Loopback] 后缀、去控制字符、压空格、转小写。"""
        s = _clean_name(str(name or "").replace("[Loopback]", " "))
        return re.sub(r"\s+", " ", s).strip().lower()

    def _pick_pa_loopback(self, candidates: List[dict]) -> dict:
        """在 PyAudio 的回环设备里挑出选中的那块。

        1. `self.device` 本身就是 PyAudio 回环索引 → 直接用（设备清单里的回环项
           存的就是这个索引，所以**用户选哪块就采哪块**，不会串）；
        2. 否则把它当 sounddevice 索引，按名字匹配（兼容 CLI / 老调用）；
        3. 都没有 → 默认输出那条，再没有才用第一条（并明确写日志）。
        """
        by_index: Dict[int, dict] = {}
        for d in candidates:
            try:
                by_index[int(d["index"])] = d
            except Exception:  # noqa: BLE001
                continue
        if self.device is not None:
            try:
                if int(self.device) in by_index:
                    return by_index[int(self.device)]
            except Exception:  # noqa: BLE001
                pass

        target = ""
        if self.device is not None:
            try:
                target = self._norm(sd.query_devices(self.device).get("name", ""))
            except Exception:  # noqa: BLE001
                target = ""
        if target:
            for d in candidates:                     # 完全相等优先
                if self._norm(d.get("name", "")) == target:
                    return d
            for d in candidates:                     # 再允许互相包含（后缀差异）
                nm = self._norm(d.get("name", ""))
                if nm and (target in nm or nm in target):
                    return d
        try:
            default_out = int(sd.default.device[1])
        except Exception:  # noqa: BLE001
            default_out = -1
        if default_out >= 0:
            try:
                dname = self._norm(sd.query_devices(default_out).get("name", ""))
            except Exception:  # noqa: BLE001
                dname = ""
            for d in candidates:
                if dname and self._norm(d.get("name", "")) == dname:
                    return d
        pick = candidates[0]
        logger.warning(
            "没匹配到「%s」的回环设备，改用「%s」。可采的只有：%s",
            target or "默认输出",
            str(pick.get("name", "")).replace("[Loopback]", "").strip(),
            "、".join(str(d.get("name", "")).replace("[Loopback]", "").strip()
                     for d in candidates))
        return pick

    def _open_loopback_pyaudio(self) -> None:
        """用 pyaudiowpatch 打开 WASAPI 回环（= 录屏软件里的「系统声音」）。

        WASAPI 对「采样率」和「缓冲大小」很挑：设备混音格式常是 48k，但蓝牙 / HDMI
        可能是 44.1k 或 96k，用错直接 `-9999 Unanticipated host error`。
        所以按「设备默认 → 48k → 44.1k」×「缓冲 1024 / 2048 / 4096」依次试，
        每次失败都重建 PyAudio 实例（-9999 会留下脏状态）。
        """
        import pyaudiowpatch as pyaudio

        pa = _shared_pyaudio()
        candidates = list(pa.get_loopback_device_info_generator())
        if not candidates:
            raise RuntimeError(
                "PyAudio 没看到任何「WASAPI 回环」设备。请确认：\n"
                "① 任务栏音量图标里的输出设备是你在用的那块（耳机/HDMI/扬声器）；\n"
                "② 那块设备没被禁用、也没被别的程序独占。")
        pick = self._pick_pa_loopback(candidates)
        dev_index = int(pick["index"])
        dev_rate = int(pick.get("defaultSampleRate") or 48000)
        channels = min(2, int(pick.get("maxInputChannels") or 2)) or 1
        name = str(pick.get("name", "")).replace("[Loopback]", "").strip()

        rates: List[int] = []
        for r in (dev_rate, 48000, 44100):
            if r and int(r) not in rates:
                rates.append(int(r))
        buffers: List[int] = []
        for b in (max(512, int(self.blocksize)), 2048, 4096):
            if int(b) not in buffers:
                buffers.append(int(b))

        self._pa_continue = pyaudio.paContinue
        errors: List[str] = []
        for rate in rates:
            for buf in buffers:
                stream = None
                try:
                    stream = pa.open(
                        format=pyaudio.paFloat32,
                        channels=channels,
                        rate=rate,
                        input=True,
                        input_device_index=dev_index,
                        frames_per_buffer=buf,
                        stream_callback=self._pa_callback,
                    )
                    # pyaudiowpatch 在 Stream 构造里就会 start；万一没启动再补一次
                    if hasattr(stream, "is_active") and not stream.is_active():
                        stream.start_stream()
                except Exception as exc:  # noqa: BLE001  换一组参数继续试
                    errors.append(f"{rate}Hz/{buf}: {type(exc).__name__}: {exc}")
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:  # noqa: BLE001
                            pass
                    logger.info("回环开流失败（%d Hz / %d）：%s", rate, buf, exc)
                    _reset_pyaudio()          # -9999 会留下脏状态，重建再试
                    pa = _shared_pyaudio()
                    continue
                self.samplerate = rate
                self._pa = pa
                self._pa_stream = stream
                self._pa_buffer = buf
                self._loop_source = f"PyAudio · {name}"
                logger.info("系统声音回环已打开：%s（%d Hz / %d 声道 / 缓冲 %d）",
                            name, rate, channels, buf)
                return

        detail = "\n".join(errors[-4:]) if errors else "（没有可用组合）"
        raise RuntimeError(
            f"打不开「{name}」的系统声音回环。试过 {len(errors)} 种采样率/缓冲组合都失败。\n"
            f"最后一次的错误：\n{detail}\n\n"
            "常见原因与对策：\n"
            "① 这块输出设备正被别的程序独占（某些游戏/语音软件的独占模式）"
            "→ 关掉它们，或在 Windows 声音设置里关掉「独占模式」；\n"
            "② 设备刚切换/刚插拔 → 等两秒，点「🎯 找正在出声的」重新识别；\n"
            "③ 系统里有多块声卡且默认输出不在用 → 把任务栏音量切到真正出声的那块；\n"
            "④ 实在不行就在下拉里换一个 🎵 设备，或改用手动统一延迟。")

    def _pa_callback(self, in_data, frame_count, time_info, status):
        """PyAudio 回调：算 RMS + 存波形，逻辑与 sounddevice 回调一致。"""
        try:
            now = time.perf_counter()
            arr = np.frombuffer(in_data, dtype=np.float32)
            if arr.size:
                if arr.size % 2 == 0 and arr.size // 2 == frame_count:
                    arr = arr.reshape(-1, 2).mean(axis=1)      # 立体声 → 单声道
                arr = arr.astype(np.float32)
                rms = float(np.sqrt(np.mean(np.square(arr))))
                if self._collect:
                    # 与 sounddevice 回调保持一致：存**块起点**（回调发生在块录满之后，
                    # 直接存 now 会让 grab 的每段整体晚一个块长）
                    start = now - (float(frame_count) / float(self.samplerate or 48000))
                    self._audio.append((start, arr.copy()))
                self._q.put((now, rms))
        except Exception:  # noqa: BLE001
            pass
        return (None, self._pa_continue)

    def open(self):
        self._stop.clear()
        self._audio.clear()          # 每键重新开流，别把上一键的余音混进来
        if self.device is None:
            self.device = self._resolve_device()
        if self.loopback:
            # 系统声音必须走 WASAPI 回环（sounddevice 做不到，靠 pyaudiowpatch）
            self._assert_loopback_supported()
            self._open_loopback_stream()
            return
        kwargs = dict(
            device=self.device,
            samplerate=self.samplerate,
            blocksize=self.blocksize,
            channels=1,
            callback=self._callback,
        )
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
        # PyAudio 回环
        st, self._pa_stream = getattr(self, "_pa_stream", None), None
        if st is not None:
            try:
                st.stop_stream()
                st.close()
            except Exception:  # noqa: BLE001
                pass
        # 注意：**不要** pa.terminate()。实例是全局共享的，terminate 掉下一次
        # 还得重新 Initialize，WASAPI 在这种来回里就是 -9999 的来源。
        self._pa = None
        self._audio.clear()
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
        """采集静默基线：返回 (安静电平, 稳健波动)。

        **为什么安静电平不用均值**：上一键的琴音余音常常还没衰减完（钢琴音色能响一秒多），
        这 12 块里全是正在衰减的声音 → 均值被拉高 → 阈值跟着抬高 → 下一键明明响了
        也判成「没检测到」。实测表现就是**隔一个漏一个**（Z✓ X✗ C✓ V✗ …）。
        所以取 25 分位数当「最安静时有多静」，它对衰减尾巴不敏感。

        **为什么波动用 MAD 而不是标准差**（2026-10-31 修）：标准差对离群值极其敏感。
        12 块里只要有**一块**上一键的余音（0.05，其余 0.001），std 就有 0.0135，而
        `wait_onset` 的阈值含 `mean + 4×std` → 抬到 0.055；正常琴声只有 0.02~0.05，
        于是被判「未检测到（跳过）」。用户说的「总是有几个键没采到」就是这个。
        MAD（中位数绝对偏差）对单块离群几乎无感：同样数据 MAD≈0 → 阈值 0.0025。
        """
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
        quiet = float(np.percentile(arr, 25))
        med = float(np.median(arr))
        mad = float(np.median(np.abs(arr - med)))
        return quiet, mad

    def wait_onset(
        self,
        after: float,
        timeout: float = 0.8,
        baseline_mean: float = 0.0,
        baseline_std: float = 0.0,
    ) -> Optional[float]:
        """从 after（perf_counter 时刻）之后等待声音 onset，返回 onset 时刻；超时返回 None。

        两种触发条件（任一满足即算发声）：
        1. **绝对**：能量超过 `安静电平 × 2.5`（余音已散时的正常情形）；
        2. **相对**：能量比前几块突然涨 1.5 倍 —— 上一键的余音还在响时，新音效叠上去
           是「回升」而不是从静音起步，只靠绝对阈值会漏检（就是「隔一个漏一个」）。

        `baseline_std` 传的是**稳健波动（MAD）**，不是标准差 —— 见 `baseline()`：
        用 std 时一块余音就能把 floor 抬到正常琴声之上，表现为「总是有几个键没采到」。
        本次等待的峰值 / 阈值都记在 `last_peak` / `last_floor` 上，失败时好判断
        到底是「没出声」还是「算法没抓到」。
        """
        # 绝对下限别设太低：MAD≈0 时 floor 会整个塌到这个下限上，而 1.2e-4 连
        # 蓝牙音箱的底噪/背景音乐都盖不住 —— 于是「随便一声响」都算这一键发声，
        # 实测延迟飘到 29~854ms（同一设备原来稳定在 37~122ms）。取 2e-3：
        # 比典型底噪（1e-4 量级）高一个数量级，比正常琴声（0.01~0.05）低一个数量级。
        floor = max(baseline_mean * 2.5, baseline_mean + 4.0 * baseline_std, 2e-3)
        self.last_floor = floor
        self.last_peak = 0.0
        deadline = time.perf_counter() + timeout
        recent: List[float] = []
        while time.perf_counter() < deadline:
            try:
                t, rms = self._q.get(timeout=0.05)
            except Exception:  # noqa: BLE001
                continue
            if rms > self.last_peak:
                self.last_peak = rms
            if t < after:
                recent.append(rms)
                continue
            prev = recent[-3:]
            prev_avg = (sum(prev) / len(prev)) if prev else 0.0
            if rms >= floor:
                return t
            if prev_avg > 0 and rms > prev_avg * 1.5 and rms > 6e-4:
                return t              # 余音里突然变响 = 新音效
            recent.append(rms)
        return None

    def probe(self, seconds: float = 1.0) -> dict:
        """录一小段，报告**这次到底有没有数据进来**。

        这是采样前的自检，用来把「设备选错 / 流没启动 / 采到的全是零」这三种情况分开：
        - `blocks == 0`：回调一次都没触发 → 流没真正跑起来（不是音量问题）；
        - `blocks > 0` 但 `peak == 0.0`：有数据但全是零 → 采的不是正在出声的那块设备，
          或者那块设备被静音了；
        - `peak` 只有 1e-5 量级 → 有底噪，设备在工作，只是此刻没声音（正常，等按键）。

        没有这一步的话，21 个键跑完只会得到一句「全部未检测到」，用户根本无从下手。
        """
        self._drain_old()
        peak = 0.0
        total = 0.0
        n = 0
        nonzero = 0
        deadline = time.perf_counter() + max(0.2, float(seconds))
        while time.perf_counter() < deadline:
            try:
                _, rms = self._q.get(timeout=0.1)
            except Exception:  # noqa: BLE001
                continue
            n += 1
            if rms > 0.0:
                nonzero += 1
            if rms > peak:
                peak = rms
            total += rms
        return {
            "blocks": n,
            "nonzero": nonzero,
            "peak": peak,
            "mean": (total / n) if n else 0.0,
            "samplerate": int(self.samplerate or 0),
            "device": self.device_name,
            "audio_blocks": len(self._audio),
        }

    def wait_quiet(self, quiet: float = 0.0, timeout: float = 1.6,
                   rel: float = 2.5) -> None:
        """等上一键的余音衰减掉，最多等 timeout 秒。

        每键之间只 `sleep(0.25)` 是不够的：钢琴音色余音长，下一键的基线会被污染。
        这里等到能量回落到「安静电平 × rel」以下再返回（超时就继续，不卡死）。

        **阈值下限为什么是 6e-4 而不是 2e-4**（2026-10-31）：钢琴余音按指数衰减，
        要掉到 2e-4 往往要一秒半以上；原来的 1.0 秒超时几乎每次都等不到就放行，
        下一键的基线里全是衰减尾巴 → 阈值偏高 → 那个键被判「未检测到」。
        抬到 6e-4（仍是 −64 dBFS 量级的极静）并给到 1.6 秒，实测能等到真正安静。
        """
        thresh = max(quiet * rel, 6e-4)
        deadline = time.perf_counter() + max(0.0, timeout)
        while time.perf_counter() < deadline:
            try:
                _, rms = self._q.get(timeout=0.05)
            except Exception:  # noqa: BLE001
                continue
            if rms < thresh:
                return


# 枚举时的 hostapi 优先级：同一个物理设备会在 MME / DirectSound / WASAPI / WDM-KS 各出现
# 一次，只留一个（优先 WASAPI：延迟低、支持系统声音回环）
_API_PRIORITY = {"Windows WASAPI": 0, "Windows WDM-KS": 1, "MME": 2, "Windows DirectSound": 3}

# 这些是 Windows 的聚合/占位设备，选了也录不到具体声音
_JUNK_NAMES = ("主声音驱动程序", "primary sound driver", "microsoft 声音映射器",
               "microsoft sound mapper", "output ()", "input ()", "output (", "input (")


def _clean_name(name: str) -> str:
    """设备名里常带换行/制表/零宽字符（蓝牙设备尤其多），统一压成一行空格。"""
    s = str(name or "")
    for ch in ("\r", "\n", "\t", "\u200b", "\u200e", "\u200f"):
        s = s.replace(ch, " ")
    while "  " in s:
        s = s.replace("  ", " ")
    return s.strip()


def _dedup_key(name: str) -> str:
    """去重用的键：MME 的设备名会被截断到 31 字符，同一块声卡因此看起来像两个。

    统一取前 20 个字符（并压掉空白）就能把「LECOON2721U-2 (NVIDIA High Defi」和
    「LECOON2721U-2 (NVIDIA High Definition Audio)」认成同一块。
    """
    s = re.sub(r"\s+", " ", _clean_name(name).lower())
    return s[:20]


def _is_junk(name: str) -> bool:
    low = str(name or "").strip().lower()
    if not low:
        return True
    if any(j in low for j in _JUNK_NAMES):
        return True
    # 「耳机 ()」「扬声器 ()」这种括号里空空的，没有辨识度，只是同设备的一份副本
    return bool(re.search(r"\(\s*\)\s*$", low))


def _api_rank(hostapis: List[dict], hostapi_index: int) -> int:
    try:
        name = str(hostapis[int(hostapi_index)].get("name", ""))
    except Exception:  # noqa: BLE001
        return 9
    return _API_PRIORITY.get(name, 9)


def build_capture_list(devices: List[dict], hostapis: List[dict],
                       default_out: Optional[int] = None) -> List[dict]:
    """把 sounddevice 的原始设备表整理成「能用的捕获设备」短清单（纯函数，便于测试）。

    规则：
    1. 输出设备按**扬声器**列一条 `loopback=True` 的候选（每块声卡一条，按名字去重）。
       注意：这里只是「理论候选」——能不能真的采系统声音取决于 PyAudio 的 WASAPI
       回环列表，`list_capture_devices()` 会用 `pyaudio_loopback_devices()` 的结果
       **整个替换掉**这些候选（那个 index 才是能直接喂给 PyAudio 的编号）。
    2. 输入设备也按名字去重（同一声卡在四个 hostapi 各一份），优先 WASAPI。
    3. 过滤 Windows 的聚合占位设备（「主声音驱动程序」「Microsoft 声音映射器」等）。
    4. 顺序：先回环（用户主要要这个），再输入设备；默认输出对应的回环排最前。
    """
    loops: List[dict] = []
    seen_loops: dict = {}
    seen_inputs: dict = {}

    for i, d in enumerate(devices):
        name = _clean_name(d.get("name", ""))
        if _is_junk(name):
            continue
        api_rank = _api_rank(hostapis, d.get("hostapi", 0))
        sr = int(d.get("default_samplerate", 0) or 0)
        in_ch = int(d.get("max_input_channels", 0) or 0)
        out_ch = int(d.get("max_output_channels", 0) or 0)

        # 1) 回环候选：每块扬声器一条（真正的回环列表由 PyAudio 提供，见下）
        if out_ch > 0:
            key = _dedup_key(name)
            prev = seen_loops.get(key)
            if prev is None or api_rank < prev["_rank"]:
                seen_loops[key] = {
                    "index": i, "name": f"🎵 系统声音(回环) · {name}",
                    "raw_name": name, "samplerate": sr, "loopback": True,
                    "_rank": api_rank,
                    "is_default_out": (i == default_out),
                }

        # 2) 输入设备：按名字去重，保留优先级最高的那个 hostapi
        if in_ch > 0:
            key = _dedup_key(name)
            prev = seen_inputs.get(key)
            if prev is None or api_rank < prev["_rank"]:
                seen_inputs[key] = {
                    "index": i, "name": f"🎤 {name}", "raw_name": name,
                    "samplerate": sr, "loopback": False, "_rank": api_rank,
                }

    loops = [item for item in seen_loops.values()]
    inputs = [{k: v for k, v in item.items() if k != "_rank"}
              for item in seen_inputs.values()]
    # 回环里把「默认输出的那块」提到最前，用户一般就是要它
    loops.sort(key=lambda d: (0 if d.get("is_default_out") else 1, d["_rank"], d["index"]))
    inputs.sort(key=lambda d: d["index"])
    for item in loops:
        item.pop("is_default_out", None)
        item.pop("_rank", None)
    return loops + inputs


def pyaudio_loopback_devices() -> List[dict]:
    """pyaudiowpatch 能**实际采集**的回环设备（index 是 PyAudio 的编号）。

    这是设备清单里「🎵 系统声音(回环)」的唯一来源：只有这里列出来的扬声器才真能采，
    其余的选了要么开不了流、要么静默退到别的设备（用户以为换了，其实没换）。

    返回 `[{index, name, raw_name, samplerate, channels}]`，默认输出的那块排最前。
    """
    try:
        import pyaudiowpatch as pyaudio
    except Exception:  # noqa: BLE001  没装就没有回环项（UI 会提示去装）
        return []
    pa = None
    out: List[dict] = []
    try:
        pa = pyaudio.PyAudio()
        for d in pa.get_loopback_device_info_generator():
            raw = _clean_name(str(d.get("name", "")).replace("[Loopback]", ""))
            if _is_junk(raw):
                continue
            out.append({
                "index": int(d.get("index")),
                "name": f"🎵 系统声音(回环) · {raw}",
                "raw_name": raw,
                "samplerate": int(d.get("defaultSampleRate") or 48000),
                "channels": min(2, int(d.get("maxInputChannels") or 2)) or 1,
                "loopback": True,          # 与输入项统一字段（下游按它分后端）
            })
    except Exception:  # noqa: BLE001
        logger.warning("枚举 PyAudio 回环设备失败", exc_info=True)
        return []
    finally:
        if pa is not None:
            try:
                pa.terminate()
            except Exception:  # noqa: BLE001
                pass
    return out


def pyaudio_loopback_names() -> List[str]:
    """回环设备名（去掉 [Loopback] 后缀），供诊断/日志用。"""
    return [d["raw_name"] for d in pyaudio_loopback_devices()]


def loopback_capable_names() -> List[str]:
    """能采集系统声音的扬声器名（= PyAudio 回环列表）。"""
    return pyaudio_loopback_names()


def list_capture_devices() -> List[dict]:
    """列出可用于「采到模拟器声音」的捕获设备（已去重、已过滤占位设备）。

    - `loopback=True`：系统声音回环（WASAPI loopback，**index 是 PyAudio 的编号**），
      即录屏软件里说的「系统声音」，不需要麦克风、不需要虚拟声卡；
    - `loopback=False`：真实输入设备（麦克风 / 立体声混音），**index 是 sounddevice 的编号**。

    返回值每项：`{index, name, raw_name, samplerate, loopback}`。
    两套 index 各自配套自己的后端，`AudioMonitor(loopback=...)` 会按模式分别使用，
    不要混着传（混了会采到别的设备）。
    """
    inputs: List[dict] = []
    if _HAS_SD:
        try:
            devices = list(sd.query_devices())
            hostapis = list(sd.query_hostapis())
            items = build_capture_list(devices, hostapis)
            inputs = [d for d in items if not d.get("loopback")]
        except Exception:  # noqa: BLE001
            inputs = []

    loops = pyaudio_loopback_devices()
    if not loops:
        return inputs          # 没有回环能力就只列输入（UI 会提示装 pyaudiowpatch）

    # 系统默认输出的那块排最前（用户一般就是要它）
    default_key = ""
    if _HAS_SD:
        try:
            out_idx = int(sd.default.device[1])
            if out_idx >= 0:
                default_key = _dedup_key(_clean_name(
                    sd.query_devices(out_idx).get("name", "")))
        except Exception:  # noqa: BLE001
            default_key = ""
    if default_key:
        loops.sort(key=lambda d: (0 if _dedup_key(d["raw_name"]) == default_key else 1,
                                  d["index"]))
    return loops + inputs


def probe_loopback_levels(devices: List[dict], seconds: float = 0.45,
                          samplerate: int = 48000) -> List[dict]:
    """逐个回环设备测一小段电平，用来找「现在正在出声的那块」。

    多屏/多声卡时很容易选错（声音从笔记本喇叭出，却选了 HDMI 显示器），
    结果 21 个键全采成静音、白等一分钟。这里让程序自己听一下谁有声音。

    **和实际采集走同一个后端**（`quick_peak_level` → AudioMonitor），
    否则「探测说有声音、采样却采不到」会更让人迷惑。

    返回 `[{index, name, peak, error?}]`，peak 接近 0 表示这块没在出声。
    """
    out: List[dict] = []
    seen: set = set()
    for d in devices:
        if not d.get("loopback"):
            continue
        key = _dedup_key(d.get("raw_name") or d.get("name", ""))
        if key in seen:
            continue
        seen.add(key)
        item = {"index": d.get("index"), "name": d.get("name", ""), "peak": 0.0}
        try:
            item["peak"] = quick_peak_level(device=d.get("index"), loopback=True,
                                            seconds=seconds)
        except Exception as exc:  # noqa: BLE001  单个设备失败不影响别的
            item["error"] = f"{type(exc).__name__}: {exc}"
        out.append(item)
    return out


def quick_peak_level(device: Optional[int] = None, loopback: bool = False,
                     seconds: float = 0.5) -> float:
    """开一次流听 0.5 秒，返回峰值（采样前的静音预检）。"""
    try:
        mon = AudioMonitor(device=device, loopback=loopback)
        mon.open()
    except Exception:  # noqa: BLE001
        return 0.0
    peak = 0.0
    try:
        deadline = time.perf_counter() + max(0.2, float(seconds))
        while time.perf_counter() < deadline:
            try:
                _, rms = mon._q.get(timeout=0.1)
                peak = max(peak, float(rms))
            except Exception:  # noqa: BLE001
                continue
    finally:
        try:
            mon.close()
        except Exception:  # noqa: BLE001
            pass
    return peak


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
    timeout: float = 1.0,
    post_pause: float = 0.25,
    clip_hook: Optional[Callable[["np.ndarray", int], None]] = None,
    clip_pre: float = 0.05,
    clip_post: float = 0.85,
    keep_open: bool = False,
    label: str = "",
) -> Optional[float]:
    """点击 (x, y) 处的琴键，返回「点击→发声」延迟（秒）；失败返回 None。

    click_fn: 实际执行点击的函数（默认 pyautogui.click(x, y, duration=0, _pause=False)）。
    clip_hook(clip, samplerate): 检测到发声后，把「发声音色」交给它（给「模拟音」用）；
    monitor 必须以 `collect_audio=True` 构造，否则拿不到波形。
    keep_open: 真 = 复用外面已经打开的流（批量采样时必须这样：每个键都开关流在
        WASAPI 上极易抛 -9999 Unanticipated host error，而且慢得离谱）。
    label: 日志里怎么称呼这个键（键盘模式传键名，别写成坐标）。
    """
    opened_here = True
    if keep_open and bool(getattr(monitor, "is_open", False)):
        opened_here = False
        monitor._drain_old()          # 清掉上一键的余音
    else:
        monitor.open()
    mean, std = 0.0, 0.0
    try:
        mean, std = monitor.baseline(blocks=12)
        # 清掉基线期间可能残留的块
        monitor._drain_old()
        t_click = time.perf_counter()
        click_fn(x, y)
        onset = monitor.wait_onset(after=t_click, timeout=timeout, baseline_mean=mean, baseline_std=std)
        if onset is None:
            peak = float(getattr(monitor, "last_peak", 0.0) or 0.0)
            floor = float(getattr(monitor, "last_floor", 0.0) or 0.0)
            # 峰值远低于阈值 = 这个键根本没发出声音（没绑定 / 被吞 / 静音）；
            # 峰值明显高于阈值却没触发 = 采样窗口错过了（游戏卡了一下）。
            # 两种情况处理方式完全不同，日志里必须分开，否则用户只能瞎猜。
            if peak < floor:
                why = "完全没声音（键没绑定 / 按键被吞 / 静音）"
            else:
                why = f"有声音（峰值 {peak:.4f} ≥ 阈值 {floor:.4f}）但没在 {timeout:g}s 内抓到起点"
            logger.warning("%s 未检测到发声：%s", label or f"键 ({x}, {y})", why)
            return None
        if clip_hook is not None:
            try:
                # **必须等够 clip_post 秒再截**：onset 刚检测到的那一刻，录音缓冲里
                # 只有刚发生的几十毫秒；立刻 grab_clip 会存出 10~20ms 的碎片，
                # 浏览器里播出来就是「咔」一声（用户说的「像捡碎片」就是这个）。
                remain = (onset + clip_post) - time.perf_counter()
                if remain > 0:
                    time.sleep(min(remain, clip_post))
                clip = monitor.grab_clip(onset, pre=clip_pre, post=clip_post)
                if clip is not None and len(clip):
                    clip_hook(clip, int(monitor.samplerate))
            except Exception:  # noqa: BLE001  录不到音色不该影响延迟测量
                logger.warning("截取发声音色失败", exc_info=True)
        return max(0.0, onset - t_click)
    finally:
        if opened_here:
            time.sleep(post_pause)  # 单键模式：睡一下就关流
            monitor.close()
        else:
            # 复用流（批量采样）：不能固定睡 0.25s 就测下一键 —— 钢琴音色余音能响
            # 一秒多，下一键的基线会被它抬高，于是「隔一个漏一个」。等真正安静。
            wq = getattr(monitor, "wait_quiet", None)
            try:
                if wq is not None:
                    wq(quiet=mean, timeout=max(1.6, post_pause * 4))
                else:
                    time.sleep(post_pause)
            except Exception:  # noqa: BLE001
                time.sleep(post_pause)
