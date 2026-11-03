"""⑤-b 逐键延迟采样：把 21 个键的「点击→发声」延迟测出来，存成样本库。

样本库（key_latency.json）会被播放器读取，用于逐键提前补偿。
"""
from __future__ import annotations

import json
import logging
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .latency_probe import AudioMonitor, measure_key_latency, pick_monitor_device
from .models import Calibration

logger = logging.getLogger("piano_tool.calibrate_latency")


def analyze_onset(clip, samplerate: int, frame_ms: float = 5.0, min_run: int = 3):
    """在「按下之后录到的一整段」里找发声起点，返回相对这段起点的秒数；找不到返回 None。

    **为什么改成事后整段分析**（2026-10-31）：老做法是实时等「某一块能量超过阈值」，
    阈值要同时对付上一键余音、背景音乐、蓝牙底噪 —— 调高一格就漏检、调低一格就把
    无关的响声当发声，实测延迟从 37~122ms 散成 29~854ms，怎么调都不对。

    整段分析就不需要跨键可比了：**用这一键自己的动态范围定阈值**

        thresh = 背景 + (本段峰值 - 背景) × 0.30

    等于问「能量从这一段最低水平上升了 30% 没有」，哪怕整段都压在背景音乐上也成立。
    找到之后还要**连续 3 帧**都超过才算（挡掉单个毛刺），再往前回退到能量刚开始抬的地方。
    """
    import numpy as np

    if clip is None:
        return None
    data = np.asarray(clip, dtype=np.float32).ravel()
    if data.size < 8:
        return None
    step = max(8, int(samplerate * frame_ms / 1000.0))
    frames = data.size // step
    if frames < max(4, min_run + 1):
        return None

    rms = np.sqrt(np.mean(data[: frames * step].reshape(frames, step) ** 2, axis=1))
    peak = float(rms.max())
    if peak < 2e-3:                      # 整段都够不上「有声音」→ 这个键确实没响
        return None
    bg = float(np.percentile(rms, 20))    # 这一段的背景水平（余音/底噪/背景音乐都算）
    if peak < bg * 1.25:
        # 整段都差不多响 = 只有背景音乐/底噪，没有「新加进来的声音」。
        # 这一条专治「游戏没响但背景音乐一直在放」——不判的话会把音乐当成按键声。
        return None
    thresh = max(bg + (peak - bg) * 0.30, 2e-3)

    run = 0
    for i, v in enumerate(rms):
        if v >= thresh:
            run += 1
            if run >= min_run:
                start = i - min_run + 1
                # 往前退到「能量刚开始离开背景」的位置，别把起音那段切掉
                while start > 0 and rms[start - 1] > bg * 1.3:
                    start -= 1
                return start * step / float(samplerate)
        else:
            run = 0
    return None


def sanitize_latencies(result: Dict[str, Optional[float]]):
    """把明显离谱的延迟踢掉，用中位数代替；返回 `(改了哪些键, 中位数秒)`。

    **为什么需要这一步**：蓝牙 / 网络音箱（用户那块「小爱触屏音箱」就是）的回环延迟
    会飘到几百毫秒且逐键抖动。而这个值在播放时是**提前多久按键**的依据 ——
    854ms 的提前量会让整首曲子明显错拍，比统一用中位数糟糕得多。

    分两步，**顺序不能反**：

    1. 先按物理上限砍掉 >450ms 的（真实「点击→发声」再慢也到不了，蓝牙/网络音箱
       最坏 300 多）。这一步必须先做：如果一半的键都测错了（实测 29~854ms 双峰），
       中位数会被拉到 357ms，直接拿它当基准会把正确的 29~60ms 也判成离群。
    2. 用**剩下的**值算中位数，再做相对过滤（中位数 ×2 +150ms，且不超 450ms）。

    被踢掉的键用第 2 步的中位数填充 —— 也就是「其余键测出来的正常值」。
    """
    vals = {k: v for k, v in result.items() if v is not None and v >= 0}
    if len(vals) < 4:
        return [], None
    survivors = {k: v for k, v in vals.items() if v <= 0.45}
    if len(survivors) < 3:
        # 几乎全是离谱值（设备/环境不对）：退而求其次，取最小的那一簇当基准
        ordered = sorted(vals.items(), key=lambda kv: kv[1])
        survivors = dict(ordered[:max(3, len(vals) // 3)])
    med = float(statistics.median(survivors.values()))
    hi = min(med * 2.0 + 0.15, 0.45)     # 上限：相对 + 绝对，取更严的那个
    lo = max(0.0, med * 0.5 - 0.05)
    changed = []
    for k, v in vals.items():
        if v > hi or v < lo:
            changed.append((k, v, med))
            result[k] = med
    return changed, med


def sample_all_keys(
    calibration: Calibration,
    monitor: Optional[AudioMonitor] = None,
    click_fn: Optional[Callable[[float, float], None]] = None,
    key_fn: Optional[Callable[[str], None]] = None,
    on_each: Optional[Callable[[str, Optional[float]], None]] = None,
    timeout: float = 1.0,          # 旧参数，保留兼容（新流程按窗口整段分析，不再用它）
    sample_dir=None,
    interval: float = 1.25,        # 每键间隔（秒）：按键 → 录满 → 等余音散 → 下一键
    window: float = 0.95,          # 每键录多长（秒），必须 > interval 才不重叠
) -> Dict[str, Optional[float]]:
    """逐键测量延迟。返回 {键名: 延迟(秒) 或 None}。

    **工作方式（2026-10-31 换成固定节拍 + 事后整段分析）**：
    按固定间隔依次按键，每一下都录满 `window` 秒，然后**离线**在这一段里找发声起点。
    老做法是实时等「能量超阈值」，阈值要同时对付余音/背景音乐/底噪，怎么调都两头堵
    （调高漏检、调低把无关响声当发声）。整段分析只看这一段自己的动态范围，稳得多。
    

    monitor: AudioMonitor 实例；为 None 时自动构造（默认监听设备）。
    click_fn: 实际点击函数，默认 pyautogui.click(x, y, duration=0, _pause=False)。
              屏幕校准（鼠标点击）方式用这个。
    key_fn:  按「键名」触发的按键函数（键盘映射方式专用），给了就忽略坐标。
    on_each(key, latency): 每测完一个键回调，便于 GUI 刷新进度。
    sample_dir: 给了就把每键**真实发出的声音**顺手录下来存成 wav（`<键>.wav`），
                供浏览器编辑器「模拟音」当音色用；不给就只测延迟。
    """
    audio_dir = Path(sample_dir) if sample_dir else None
    if audio_dir is not None:
        # 采样期间必须留住波形（默认只算 RMS，波形用完就丢）
        if monitor is None:
            monitor = AudioMonitor(device=pick_monitor_device(), collect_audio=True)
        else:
            monitor._collect = True
        audio_dir.mkdir(parents=True, exist_ok=True)
        for old in audio_dir.glob("*.wav"):    # 重新采样就覆盖旧的整套
            try:
                old.unlink()
            except OSError:
                pass

    if monitor is None:
        monitor = AudioMonitor(device=pick_monitor_device())
    if click_fn is None:
        import pyautogui

        def click_fn(x, y):
            pyautogui.click(x, y, duration=0, _pause=False)

    result: Dict[str, Optional[float]] = {}
    clips: Dict[str, tuple] = {}
    state = {"samplerate": int(getattr(monitor, "samplerate", 0) or 0)}

    # ---- 整轮只开一条流 ----------------------------------------------------
    # 老实现每个键都 open/close：21 个键 = 42 次「初始化 → 起流 → 关流」，
    # WASAPI 在这种来回里会抛 -9999 Unanticipated host error，而且一旦某键失败，
    # 后面每个键都跟着失败（日志里一屏「延迟=nan」）。这里开一次、跑完再关。
    opened_here = False
    if not bool(getattr(monitor, "is_open", False)):
        monitor.open()          # 开不了就直接抛，让上层给出明确提示
        opened_here = True
    logger.info("采样监听已就绪：%s（%s Hz）",
                getattr(monitor, "device_name", "?"),
                int(getattr(monitor, "samplerate", 0) or 0))

    # ---- 采样前自检：先让设备自己证明「我能收到数据」 ----------------------
    # 之前 21 个键跑完只得到一句「全部未检测到」，用户完全不知道卡在哪一环。
    # 这里先录 1 秒，把三种情况分开：流没回调 / 数据全零 / 只有底噪。
    probe = {}
    if hasattr(monitor, "probe"):
        try:
            probe = monitor.probe(seconds=1.0)
        except Exception:  # noqa: BLE001  自检失败不该拦住采样
            logger.warning("采样前自检失败（继续采样）", exc_info=True)
            probe = {}
    if probe:
        logger.info("自检：收 %d 块（非零 %d），峰值 %.6f，波形缓冲 %d 段",
                    probe.get("blocks", 0), probe.get("nonzero", 0),
                    probe.get("peak", 0.0), probe.get("audio_blocks", 0))
        if probe.get("blocks", 0) == 0:
            raise RuntimeError(
                "采集流已经打开，但**一块数据都没收到** —— 这不是音量问题。\n"
                f"设备：{probe.get('device')}\n"
                "① 这块设备的「回环」当前没有任何音频会话；② 或它被别的程序独占。\n"
                "对策：点「🎯 找正在出声的」让它按电平自动选一块，或在 ④ 页下拉里换一个 🎵 设备。")
        if probe.get("peak", 0.0) <= 0.0:
            # 不抛：键盘模式要**按键才会响**，这 1 秒全静音很正常。
            # 真正的判断交给上层（它知道当前是鼠标还是键盘模式）。
            logger.warning("自检期间采到的数据全是零（设备：%s）—— 若是鼠标模式请检查"
                           "游戏是否在出声；键盘模式则可能只是还没按键",
                           probe.get("device"))
        if probe.get("peak", 0.0) < 1e-5:
            logger.warning("自检只听到极小的底噪（%.6f）—— 设备在工作，但此刻没声音；"
                           "采样时请保持模拟器发声", probe.get("peak", 0.0))

    misses = 0          # 连续失败数
    success = 0         # 成功数
    aborted = False
    sr_now = int(getattr(monitor, "samplerate", 0) or 48000)
    state["samplerate"] = sr_now

    def _press(k):
        """按一下这个键：键盘模式发键名，鼠标模式点校准坐标。"""
        if key_fn is not None:
            key_fn(k.key)
        else:
            px, py = calibration.screen_point(k)
            click_fn(px, py)

    def _record(t0, seconds):
        """等这段窗口录满，把波形整段取回来（不在按键瞬间做判断）。"""
        remain = seconds - (time.perf_counter() - t0)
        if remain > 0:
            time.sleep(remain)
        try:
            return monitor.grab(t0, t0 + seconds)
        except Exception:  # noqa: BLE001  取不到波形就当这段没声音
            logger.warning("键 %s 取录音失败", getattr(k, "key", "?"), exc_info=True)
            return None

    try:
        for k in calibration.keys:
            lat: Optional[float] = None
            clip = None
            try:
                t_click = time.perf_counter()
                _press(k)
                clip = _record(t_click, window)
                if clip is None or len(clip) == 0:
                    # 这一条非常关键：取不到波形 ≠ 没声音。说明「录音缓冲里没有这段时间」，
                    # 多半是 _collect 没开、或者流的时间戳和主线程对不上。
                    logger.warning("键 %s：这段录音是空的（grab 没取到数据，不是没声音）", k.key)
                lat = analyze_onset(clip, sr_now)

                if lat is None:
                    # 这一段里确实没有「能量起来」的地方：可能是游戏偶发吞键，
                    # 隔一下再按一次确认（只重试一次，不无限纠缠）。
                    logger.info("键 %s 这一段没声音，隔一下重按一次", k.key)
                    time.sleep(0.3)
                    t_retry = time.perf_counter()
                    _press(k)
                    clip2 = _record(t_retry, window)
                    lat2 = analyze_onset(clip2, sr_now)
                    if lat2 is not None:
                        lat, clip = lat2, clip2

                if lat is not None and audio_dir is not None and clip is not None:
                    begin = max(0, int((lat - 0.05) * sr_now))   # 往前留 50ms 保起音
                    clips[k.key] = (clip[begin:], sr_now)
            except Exception as exc:  # noqa: BLE001
                logger.warning("采样键 %s 失败：%s: %s", k.key, type(exc).__name__, exc)
                lat = None
                try:                       # 流可能挂了，重建一条再继续
                    monitor.close()
                    monitor.open()
                    sr_now = int(getattr(monitor, "samplerate", 0) or sr_now)
                    state["samplerate"] = sr_now
                except Exception:  # noqa: BLE001
                    logger.error("重建录音流失败，已中止本轮采样")
                    result[k.key] = None
                    if on_each is not None:
                        on_each(k.key, None)
                    aborted = True
                    break

            result[k.key] = lat
            logger.info("键 %s 延迟=%.1f ms", k.key, (lat * 1000) if lat else float("nan"))
            if on_each is not None:
                on_each(k.key, lat)

            # 早退判断：**只在开头就一点声音都没有时**才中止（设备选错 / 窗口不在
            # 前台 / 系统静音）。中途个别键没响应是正常的（游戏没映射那个键、
            # 或那一下被吞），必须继续采完 —— 否则用户看到的就是「没采集完」。
            if lat is None:
                misses += 1
                if success == 0 and misses >= 5:
                    logger.error(
                        "开头连续 %d 个键都没采到声音（设备：%s），已中止本轮采样。"
                        "请确认：① 模拟器窗口在前台 ② 系统/游戏没静音 "
                        "③ 「测试音频」有电平 ④ 或用「🎯 找正在出声的」换设备。",
                        misses, getattr(monitor, "device_name", "?"))
                    aborted = True
                    break
            else:
                success += 1
                misses = 0

            # 固定节拍：距离这次按下 `interval` 秒再按下一个键。
            # 留够余音衰减 + 窗口收尾，这样每一键都在干净背景下录。
            rest = t_click + interval - time.perf_counter()
            if rest > 0:
                time.sleep(rest)
    finally:
        if opened_here:
            try:
                monitor.close()
            except Exception:  # noqa: BLE001
                pass

    if aborted:
        # 没测到的键显式留 None（GUI 会显示「未采集」），不用假装成功
        for k in calibration.keys:
            result.setdefault(k.key, None)
    fixed, med_sec = sanitize_latencies(result)
    if fixed:
        logger.warning(
            "有 %d 个键的延迟明显离谱，已改用中位数 %.1f ms：%s。"
            "（蓝牙/网络音箱常见：回环延迟本身就在几百毫秒上抖动；"
            "这些值会被当成「提前多久按键」，留着会让整首曲子错拍）",
            len(fixed), (med_sec or 0) * 1000,
            "、".join(f"{k} {v * 1000:.0f}ms" for k, v, _ in fixed))
    logger.info("本轮采样结束：成功 %d / %d 个键%s",
                success, len(calibration.keys), "（提前中止）" if aborted else "")

    if audio_dir is not None and clips:
        saved = 0
        for key, (clip, sr) in clips.items():
            try:
                save_wav(audio_dir / f"{key}.wav", clip, sr or state["samplerate"])
                saved += 1
            except Exception:  # noqa: BLE001  单个键写失败不影响其它键
                logger.warning("保存键 %s 的音色失败", key, exc_info=True)
        try:
            write_sample_index(audio_dir, sorted(clips), state["samplerate"])
            logger.info("已保存 %d 个键的发声音色到 %s", saved, audio_dir)
        except Exception:  # noqa: BLE001
            logger.warning("写发声音色索引失败", exc_info=True)
    return result


def save_wav(path, clip, samplerate: int) -> Path:
    """把一段 float32 波形写成 16-bit wav（浏览器 decodeAudioData 直接吃）。

    写盘前做三件让**试听**更自然的事（不然听起来像「捡碎片」）：
    1. **裁掉前导静音**：`grab_clip` 为了保住起音瞬态带了 50ms 前瞻，但那一段是
       静音；留着会让每个音听起来「慢半拍」，连奏时尤其明显。
    2. **尾部 30ms 淡出**：采样窗口末尾往往还有余音，直接归零会有「咔」的爆音。
    3. 削波归一化到 -0.98~0.98。
    """
    import numpy as np
    import soundfile as sf

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    data = np.asarray(clip, dtype=np.float32).ravel()
    if data.size:
        peak = float(np.max(np.abs(data)))
        if peak > 1e-4:
            # 也算出「起音在第几个样本」：第一个达到峰值 8% 的位置
            idx = int(np.argmax(np.abs(data) >= peak * 0.08))
            back = int(0.003 * int(samplerate or 48000))   # 往前留 3ms 保瞬态
            data = data[max(0, idx - back):]
            peak = float(np.max(np.abs(data))) if data.size else 0.0
        fade = min(int(0.03 * int(samplerate or 48000)), max(0, data.size // 4))
        if fade > 1:
            data = data.copy()
            data[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
        if peak > 0.98:
            data = data * (0.98 / peak)
    sf.write(str(out), data, int(samplerate), subtype="PCM_16")
    return out


def write_sample_index(sample_dir, keys, samplerate: int = 0) -> Path:
    """写 key_samples/index.json：记录了哪些键、采样率、时间。"""
    out = Path(sample_dir) / "index.json"
    payload = {
        "sampled_at": datetime.now().isoformat(timespec="seconds"),
        "samplerate": int(samplerate or 0),
        "keys": list(keys),
        "count": len(keys),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


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


def profile_to_lead_seconds(profile: dict,
                            max_spread_ms: float = 120.0) -> Dict[str, float]:
    """把样本库转成 {键: 提前量(秒)}。None 的项不参与（播放时回退到全局提前量）。

    2026-11-02 加抖动保护：如果样本的 **最大值-最小值 > 120ms**，整份忽略，
    返回 {}（播放时回退到统一提前量）。原因：逐键提前量会改变音与音之间的
    相对时间；实测一份 39.9~434.0ms 的样本会让同一个谱子听起来错拍/不和谐，
    而老版本没有这份样本、用统一提前量反而更和谐。超过 450ms 的样本也直接
    判为不可信（物理上「点击→发声」到不了）。
    """
    out: Dict[str, float] = {}
    for k, v in (profile.get("latencies") or {}).items():
        if v is not None:
            out[k] = float(v) / 1000.0
    if not out:
        return {}

    vals = [v * 1000.0 for v in out.values()]
    hi, lo = max(vals), min(vals)
    if hi > 450.0 or (len(vals) >= 2 and hi - lo > max_spread_ms):
        logger.warning(
            "逐键延迟样本抖动过大（%.0f~%.0f ms，跨度 %.0f ms），"
            "已忽略逐键补偿，改用统一提前量", lo, hi, hi - lo)
        return {}
    return out


def build_profile_from_constant(latency_ms: float, calibration: Calibration, path) -> Path:
    """手动兜底：用一个统一延迟值给所有键建样本库。"""
    latencies = {k.key: latency_ms / 1000.0 for k in calibration.keys}
    return save_latency_profile(
        latencies, path,
        device_name="手动统一延迟",
        notes=f"用户手动指定统一延迟 {latency_ms} ms；未使用音频自动检测",
    )
