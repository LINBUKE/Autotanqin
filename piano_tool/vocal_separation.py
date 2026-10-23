"""① 人声分离（Demucs）——「从音频转写」路线的质量升级。

场景依据：用户手里的源通常是整首混音（人声+伴奏+鼓）。basic_pitch 对混音
转写会把伴奏一起识进来，是"弹出来对不上原曲"的最大源头之一。把人声
（≈主旋律）先剥出来再转写，转写质量明显提升——这是音乐制作里的标准做法。

实现：子进程调用 demucs CLI（--two-stems=vocals，只分人声/伴奏两轨），
不碰它的 Python 内部 API（版本变动风险大）。模型 htdemucs 在首次使用时
自动下载（约 160MB），之后走本地缓存。

CLI 用法：
    python -m piano_tool.vocal_separation input.mp3 -o data/output/separated
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("piano_tool.vocal_separation")

# Windows 下防止子进程在 GUI 应用里闪黑窗
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def is_demucs_available() -> bool:
    """demucs 是否已安装（轻量探测，不触发模型下载）。"""
    try:
        import demucs  # noqa: F401
        return True
    except ImportError:
        return False


def nvidia_gpu_present() -> bool:
    """机器上有没有 NVIDIA 显卡驱动（nvidia-smi 可用即认为有）。"""
    import shutil

    return shutil.which("nvidia-smi") is not None


def cuda_available() -> bool:
    """当前 PyTorch 能不能用 CUDA（CPU 版 torch 会返回 False）。"""
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        return False


def gpu_name() -> Optional[str]:
    try:
        import torch

        if torch.cuda.is_available():
            return torch.cuda.get_device_name(0)
    except Exception:  # noqa: BLE001
        pass
    return None


# GPU 版 PyTorch（Windows CUDA 12.1）：
#   torch 2.5.1+cu121 / torchaudio 2.5.1+cu121，cp311 有 win_amd64 wheel。
#   必须与 torchaudio 成对降级（版本必须匹配，否则 demucs 加载会报 ABI 错）。
#   basic_pitch 用的是 tensorflow，Windows 上没有 GPU 版，转写永远走 CPU——
#   GPU 加速作用于最耗时的人声分离这一步。
GPU_TORCH_VERSION = "2.5.1+cu121"
GPU_INSTALL_MIRROR = "https://mirrors.aliyun.com/pytorch-wheels/cu121/"
GPU_INSTALL_FALLBACK = "https://download.pytorch.org/whl/cu121"
GPU_INSTALL_PIPI = "https://pypi.tuna.tsinghua.edu.cn/simple"


def gpu_install_cmd(fallback: bool = False) -> List[str]:
    """安装 GPU 版 PyTorch 的 pip 命令（阿里镜像找 wheel，依赖走清华 PyPI）。

    fallback=True 时改用 PyTorch 官方 wheel 源（镜像不可用时的回退）。
    """
    src = GPU_INSTALL_FALLBACK if fallback else GPU_INSTALL_MIRROR
    return [
        sys.executable, "-m", "pip", "install",
        f"torch=={GPU_TORCH_VERSION}", f"torchaudio=={GPU_TORCH_VERSION}",
        "-f", src,
        "-i", GPU_INSTALL_PIPI,
        "--no-cache-dir",
    ]


def stream_output(proc, progress) -> int:
    """逐行实时转发子进程输出，返回进程返回码。

    tqdm / pip 的进度条用 \\r 刷新（text 模式下会切成一行行），刷新极其
    频繁——按百分比 ≥10%（或 ≥10 秒）节流转发，其余普通行原样转发。
    阻塞读 + 节流必须在这里做：等进程结束才转发 = 界面像卡死。
    """
    last_pct, last_pct_at = -10, 0.0
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        if progress is None:
            continue
        m = _PCT_RE.search(line)
        if m and ("|" in line or "it/s" in line or "s/it" in line
                  or "MB" in line or "GB" in line):
            pct = int(m.group(1))
            now = time.time()
            if pct - last_pct >= 10 or now - last_pct_at >= 10:
                progress(line)
                last_pct, last_pct_at = pct, now
        else:
            progress(line)
    return proc.wait()


def _demucs_cmd() -> List[str]:
    """优先用 Scripts/demucs.exe（干净、无模块查找开销），退回 python -m。"""
    exe = Path(sys.executable).parent / ("demucs.exe" if os.name == "nt" else "demucs")
    if exe.exists():
        return [str(exe)]
    return [sys.executable, "-m", "demucs.separate"]


def _ensure_wav(audio_path: Path, decode_dir: Path) -> Path:
    """非 wav 输入先用 soundfile 解码成 wav，再交给 demucs。

    原因：demucs 读 mp3 走 torchaudio 后端，Windows 上通常要系统装 ffmpeg；
    而 libsndfile 1.1+（soundfile>=0.12）自带 mp3 解码，项目环境里就有，
    先解码可以免去用户自己装 ffmpeg。解码失败不影响流程——原样交给
    demucs，失败了还有「回退整曲转写」兜底。
    解码文件命名为 <原名>.wav（放在 decode_dir），这样 demucs 的产物
    目录名与原始曲名保持一致。
    """
    if audio_path.suffix.lower() == ".wav":
        return audio_path
    try:
        import soundfile as sf

        data, sr = sf.read(str(audio_path), always_2d=True)
        decode_dir.mkdir(parents=True, exist_ok=True)
        wav = decode_dir / (audio_path.stem + ".wav")
        sf.write(str(wav), data, sr)
        logger.info("已解码为 wav 交给分离：%s", wav)
        return wav
    except Exception as exc:  # noqa: BLE001
        logger.warning("预解码失败（将按原样交给 demucs）：%s", exc)
        return audio_path


def _audio_duration(path: Path) -> Optional[float]:
    """读音频时长（秒）；读不了（损坏/不支持）返回 None，只影响提示不影响流程。"""
    try:
        import soundfile as sf

        return float(sf.info(str(path)).duration)
    except Exception:  # noqa: BLE001
        return None


# tqdm 进度条行里抓百分比（\r 刷新在 text 模式下会被切成一行行）
_PCT_RE = re.compile(r"(\d{1,3})%")


def separate_vocals(
    audio_path,
    out_dir,
    model: str = "htdemucs",
    device: Optional[str] = None,
    progress=None,
) -> Path:
    """分离人声，返回 vocals wav 路径。

    demucs --two-stems=vocals 会在 out_dir/<model>/<曲名>/ 下生成
    vocals.wav 与 no_vocals.wav；本琴只需要人声轨。
    device: None=自动（demucs 自选，有 CUDA 用 CUDA）/ "cpu" / "cuda"。
    progress: 可选回调 (str) -> None，用于向界面日志**实时**转发 demucs
    输出（CPU 分离整曲要几分钟，绝不能等跑完才回一条，否则界面像卡死）。
    """
    audio_path = Path(audio_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # mp3 等非 wav 输入先解码（免依赖系统 ffmpeg），失败原样交给 demucs
    wav_input = _ensure_wav(audio_path, out_dir / "_decoded")

    duration = _audio_duration(wav_input)
    if progress and duration:
        progress(f"歌曲时长约 {int(duration // 60)} 分 {int(duration % 60)} 秒；"
                 f"CPU 分离大约需要 1~3 倍时长（{int(duration / 60)}~{int(duration * 3 / 60) + 1} 分钟），"
                 f"下面会显示实时进度，请耐心等待")

    cmd = _demucs_cmd() + [
        "--two-stems", "vocals",
        "-n", model,
        "-o", str(out_dir),
        str(wav_input),
    ]
    if device in ("cpu", "cuda"):
        cmd += ["-d", device]
    logger.info("人声分离开始：%s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,   # 进度条走 stderr，合并进 stdout 一起读
        text=True,                  # universal newlines：tqdm 的 \r 刷新会切成一行行
        encoding="utf-8",
        errors="replace",
        creationflags=_CREATE_NO_WINDOW,
    )

    # 进度/心跳线程：demucs 输出稀疏的阶段（模型加载/写文件）也要让界面知道
    # 还活着。10 秒一跳，静默超过 25 秒才发，避免刷屏。
    state = {"last": time.time()}
    stop = threading.Event()
    est_minutes = int(duration * 1.5 / 60) + 1 if duration else None

    def _heartbeat():
        t0 = time.time()
        while not stop.wait(10):
            if proc.poll() is not None:
                return
            idle = time.time() - state["last"]
            if idle > 25 and progress:
                extra = f"，预计共约 {est_minutes} 分钟" if est_minutes else ""
                progress(f"仍在分离中…（已等待 {int(time.time() - t0)} 秒{extra}）")

    hb = threading.Thread(target=_heartbeat, daemon=True)
    hb.start()

    try:
        if progress is not None:
            def _cb(line: str) -> None:
                state["last"] = time.time()   # 喂心跳：有输出就说明活着
                progress(line)
        else:
            _cb = None
        rc = stream_output(proc, _cb)
    finally:
        stop.set()

    if rc != 0:
        raise RuntimeError(f"Demucs 分离失败（exit {rc}），详见上方日志")

    vocals = out_dir / model / audio_path.stem / "vocals.wav"
    if not vocals.exists():
        raise RuntimeError(f"分离完成但找不到人声轨：{vocals}")
    logger.info("人声分离完成：%s", vocals)
    return vocals


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m piano_tool.vocal_separation",
        description="Demucs 人声分离（--two-stems=vocals）",
    )
    parser.add_argument("input", help="输入音频（MP3/WAV）")
    parser.add_argument("-o", "--output", default="data/output/separated",
                        help="输出目录（默认 data/output/separated）")
    parser.add_argument("-n", "--model", default="htdemucs",
                        help="分离模型（默认 htdemucs，首次使用自动下载）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    vocals = separate_vocals(args.input, args.output, model=args.model,
                             progress=lambda line: print(line))
    print(f"✓ 人声轨已导出：{vocals}")


if __name__ == "__main__":
    main()
