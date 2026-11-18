"""① 高级分离：BS-Roformer-SW 六轨分离（试听 / 多选 / 合成）。

公开模型 BS-Roformer-SW（jarredou）一次输出 6 条音轨：
    vocals / bass / drums / guitar / piano / other
比 demucs 的「人声 / 伴奏」两轨更细：可以把贝斯、鼓、吉他、钢琴单独拎出来，
勾选需要的轨合成一条干净音频再转写，减少互相互相干扰。

推理交给 pymss（pip 包）的子进程做，不 import 它的内部 API；模型从
ModelScope 下载（国内网络友好），缓存默认在 ~/.cache/pymss/models，
同一台机器只下一次（约 667MB）。

**只用 GPU**：六轨模型在 CPU 上太慢（实测 5 秒音频要 74 秒），所以本模块
不做 CPU 回退。启用前用 `gpu_status()` 检查 NVIDIA 显卡与 CUDA 版 PyTorch，
没有显卡就直接劝退、不下载组件和模型。

CLI 用法：
    python -m piano_tool.advanced_separation 输入.mp3 -o data/output/advanced
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import soundfile as sf

logger = logging.getLogger("piano_tool.advanced_separation")

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# pymss 在 Python 3.10 上的最后一个大版本（2.2+ 要求 Python 3.11+）。
PYMSS_PIN = "pymss==2.1.9"

# 六轨模型：BS-Roformer-SW（也叫 BS-Rofo-SW-Fixed）。
ADV_MODEL = "BS-Roformer-SW"

# 顺序即界面顺序：人声最常用，鼓放最后（鼓轨对 basic_pitch 转写干扰最大）。
ADV_STEM_KEYS = ("vocals", "bass", "guitar", "piano", "other", "drums")

STEM_LABELS: Dict[str, str] = {
    "vocals": "人声",
    "bass": "贝斯",
    "guitar": "吉他",
    "piano": "钢琴",
    "other": "其他",
    "drums": "鼓",
}

# 默认勾选：鼓默认不勾（转写只认音高，鼓会被 basic_pitch 识别成噪声/错音）。
DEFAULT_CHECKED = ("vocals", "bass", "guitar", "piano", "other")


def is_pymss_available() -> bool:
    """pymss 是否已安装（find_spec 探测，不 import torch，不触发模型下载）。"""
    try:
        import importlib.util

        return importlib.util.find_spec("pymss") is not None
    except Exception:  # noqa: BLE001
        return False


def nvidia_gpu_name() -> Optional[str]:
    """通过 nvidia-smi 读 NVIDIA 显卡型号；没有显卡/没有驱动返回 None。"""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=8,
            creationflags=_CREATE_NO_WINDOW,
        )
        if out.returncode != 0:
            return None
        names = [line.strip() for line in out.stdout.splitlines() if line.strip()]
        return names[0] if names else None
    except Exception:  # noqa: BLE001
        return None


def cuda_info(timeout: int = 20) -> Tuple[bool, str]:
    """子进程读 (CUDA 是否可用, torch.version.cuda)。

    主界面进程可能已经 import 过旧 torch；装完新版本后旧模块不会自动更新，
    所以每次都开子进程拿最新状态。
    """
    code = ("import torch,sys;"
            "sys.stdout.write(('1|' if torch.cuda.is_available() else '0|')"
            "+str(torch.version.cuda or ''))")
    try:
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            timeout=timeout, creationflags=_CREATE_NO_WINDOW,
        )
        if out.returncode != 0:
            return False, ""
        avail, _, cuda = out.stdout.strip().partition("|")
        return avail == "1", cuda
    except Exception:  # noqa: BLE001
        return False, ""


def cuda_ready(timeout: int = 20) -> bool:
    """当前环境里的 PyTorch 能不能真正用 CUDA。"""
    return cuda_info(timeout)[0]


def _needs_cu128(gpu_name: str) -> bool:
    """RTX 50 系（Blackwell）必须 CUDA 12.8+，cu126 会在推理时报 kernel error。"""
    return re.search(r"RTX\s*50\d0|B100|B200|GB200|Blackwell", gpu_name or "", re.I) is not None


def _cuda_tuple(text: str) -> Tuple[int, ...]:
    try:
        return tuple(int(p) for p in (text or "").split(".") if p.isdigit())
    except ValueError:
        return ()


_GPU_CACHE: Optional[Tuple[str, Optional[str]]] = None


def reset_gpu_cache() -> None:
    """装完 CUDA 版 PyTorch 后清缓存，下一次重新检测。"""
    global _GPU_CACHE
    _GPU_CACHE = None


def gpu_status(force: bool = False) -> Tuple[str, Optional[str]]:
    """检测 GPU 状态，返回 (state, 显卡名)。

    state:
      · "ok"          —— 有 NVIDIA 显卡且 CUDA 版 PyTorch 可用；
      · "gpu_no_cuda" —— 有 NVIDIA 显卡，但当前 PyTorch 是 CPU 版；
      · "no_gpu"      —— 没检测到 NVIDIA 显卡（不该下载高级分离组件）。
    """
    global _GPU_CACHE
    if _GPU_CACHE is not None and not force:
        return _GPU_CACHE
    name = nvidia_gpu_name()
    if not name:
        # 极少数环境没有 nvidia-smi，但 torch 仍可能可用
        state = "ok" if cuda_ready() else "no_gpu"
        _GPU_CACHE = (state, None)
        return _GPU_CACHE
    avail, cuda_ver = cuda_info()
    # RTX 50 系 + CUDA<12.8：虽然 cuda_available 为 True，但模型一跑就 kernel error，
    # 按「GPU 不可用」处理，让界面提示重装 cu128 版 torch。
    if avail and _needs_cu128(name) and _cuda_tuple(cuda_ver) < (12, 8):
        logger.warning("显卡 %s 需要 CUDA 12.8+，当前 torch CUDA 是 %s", name, cuda_ver or "未知")
        avail = False
    state = "ok" if avail else "gpu_no_cuda"
    _GPU_CACHE = (state, name)
    return _GPU_CACHE


def pymss_env(base=None) -> dict:
    """pymss 子进程环境：统一 UTF-8 输出 + 不缓冲，日志才能实时到界面。"""
    env = dict(os.environ if base is None else base)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    # 备用下载源（pymss 默认走 ModelScope；用户自己指定 hf-mirror 时用得上）
    if not env.get("HF_ENDPOINT"):
        env["HF_ENDPOINT"] = "https://hf-mirror.com"
    return env


def infer_command(audio_path, out_dir, device: str = "cuda",
                  model: str = ADV_MODEL) -> List[str]:
    """拼 pymss 推理命令（独立成函数，方便单元测试断言参数）。"""
    return [
        sys.executable, "-m", "pymss.cli", "infer", model,
        "-i", str(audio_path),
        "-o", str(out_dir),
        "--save-as-folder",
        "--device", device,
        "--format", "wav",
        "--wav-bit-depth", "PCM_16",
        "--download",
    ]


def stem_output_path(out_dir, audio_path, key: str) -> Path:
    """pymss 的产物路径：out/<曲名>/<曲名>_<轨>.wav。"""
    stem = Path(audio_path).stem
    return Path(out_dir) / stem / f"{stem}_{key}.wav"


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _clean(line: str) -> str:
    """去掉 tqdm/ANSI 控制符，日志里别出现乱码方块。"""
    return _ANSI_RE.sub("", line).replace("\r", "").strip()


def separate_stems(audio_path, out_dir, device: str = "cuda",
                   progress=None, model: str = ADV_MODEL) -> Dict[str, Path]:
    """六轨分离，返回 {轨名: wav 路径}；失败抛 RuntimeError。

    **只用 GPU**：device 固定 cuda，不接受 cpu（CPU 上 5 秒音频要 74 秒，
    没有实用价值）。progress: 可选回调 (str) -> None，实时转发 pymss 输出。
    """
    if device != "cuda":
        raise ValueError("高级分离只用 GPU，device 必须是 'cuda'")
    from piano_tool.vocal_separation import stream_output

    audio_path = Path(audio_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = infer_command(audio_path, out_dir, device=device, model=model)
    logger.info("高级分离开始：%s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,   # tqdm 进度在 stderr，合并后一起读
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_CREATE_NO_WINDOW,
        env=pymss_env(),
    )
    rc = stream_output(proc, (lambda line: progress(_clean(line))) if progress else None)
    if rc != 0:
        raise RuntimeError(f"高级分离失败（exit {rc}），详见上方日志")

    stems: Dict[str, Path] = {}
    missing: List[str] = []
    for key in ADV_STEM_KEYS:
        p = stem_output_path(out_dir, audio_path, key)
        if p.exists():
            stems[key] = p
        else:
            missing.append(key)
    if not stems:
        raise RuntimeError(f"分离完成但没找到任何音轨，输出目录：{out_dir}")
    if missing:
        logger.warning("以下音轨缺失：%s", "、".join(missing))
    return stems


def mix_stems(files, out_path, subtype: str = "PCM_16") -> Path:
    """把选中的多条音轨相加成一条 wav（保留原采样率，峰值超 0.99 才整体衰减）。"""
    paths = [Path(p) for p in files]
    if not paths:
        raise ValueError("没有选中任何音轨")
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError("音轨不存在：" + "、".join(missing))

    chunks = []
    sample_rate: Optional[int] = None
    for p in paths:
        data, sr = sf.read(str(p), always_2d=True, dtype="float32")
        if sample_rate is None:
            sample_rate = int(sr)
        elif int(sr) != sample_rate:
            raise ValueError(f"采样率不一致：{p} 是 {sr}Hz，之前是 {sample_rate}Hz")
        chunks.append(data)

    length = max(len(d) for d in chunks)
    channels = max(d.shape[1] for d in chunks)
    mix = np.zeros((length, channels), dtype=np.float32)
    for d in chunks:
        if d.shape[1] < channels:
            d = np.repeat(d, channels, axis=1) if d.shape[1] == 1 else \
                np.pad(d, ((0, 0), (0, channels - d.shape[1])))
        mix[: len(d)] += d

    peak = float(np.max(np.abs(mix))) if mix.size else 0.0
    if peak > 0.99:
        mix *= 0.99 / peak

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_path), mix, sample_rate, subtype=subtype)
    return out_path


def stem_duration(path) -> Optional[float]:
    """音轨时长（秒），读不出来返回 None（只用于界面显示）。"""
    try:
        return float(sf.info(str(path)).duration)
    except Exception:  # noqa: BLE001
        return None


class StemPlayer:
    """单条音轨试听：同一时间只播一条，再次 play 会先停掉上一条。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._token = 0
        self._playing = False

    @property
    def is_playing(self) -> bool:
        return self._playing

    def play(self, path) -> None:
        import sounddevice as sd

        data, sr = sf.read(str(path), always_2d=True, dtype="float32")
        self.stop()
        with self._lock:
            self._token += 1
            token = self._token
            self._playing = True

        def _run():
            try:
                sd.play(data, int(sr))
                sd.wait()
            except Exception:  # noqa: BLE001
                logger.exception("试听失败")
            finally:
                with self._lock:
                    if token == self._token:
                        self._playing = False

        threading.Thread(target=_run, daemon=True).start()

    def stop(self) -> None:
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:  # noqa: BLE001
            pass
        with self._lock:
            self._token += 1
            self._playing = False


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m piano_tool.advanced_separation",
        description="BS-Roformer-SW 六轨分离（vocals/bass/drums/guitar/piano/other）",
    )
    parser.add_argument("input", help="输入音频（MP3/WAV）")
    parser.add_argument("-o", "--output", default="data/output/advanced",
                        help="输出目录（默认 data/output/advanced）")
    parser.add_argument("--device", default="cuda", choices=("cuda",),
                        help="推理设备（高级分离只用 GPU）")
    args = parser.parse_args(argv)

    state, gpu = gpu_status()
    if state == "no_gpu":
        raise SystemExit("未检测到 NVIDIA 显卡：高级分离只用 GPU，请不要下载组件和模型。")
    if state == "gpu_no_cuda":
        raise SystemExit(
            f"检测到 NVIDIA 显卡（{gpu or 'NVIDIA GPU'}），但当前 PyTorch 是 CPU 版；"
            "请先安装 CUDA 版 PyTorch。")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    stems = separate_stems(args.input, args.output, device=args.device,
                           progress=lambda line: print(line))
    for key, path in stems.items():
        print(f"✓ {STEM_LABELS.get(key, key)}：{path}")


if __name__ == "__main__":
    main()
