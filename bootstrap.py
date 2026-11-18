#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境引导：在 .venv 里安装/修复 requirements.txt 依赖。

由 launcher.ps1 用 .venv 的解释器调用。特点：
  - 多个国内镜像源依次回退（清华 -> 阿里云 -> 中科大 -> 华为云 -> 官方 PyPI）
  - 每个源失败自动加 --trusted-host 重试（应对代理/安全软件做 HTTPS 检查）
  - 装完做“真实导入冒烟”，抓出“包元数据在但 DLL 加载失败”的损坏安装
"""
from __future__ import annotations

import importlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQUIREMENTS = ROOT / "requirements.txt"

# (显示名, 基础URL, 主机名)
MIRRORS = [
    ("清华大学", "https://pypi.tuna.tsinghua.edu.cn/simple", "pypi.tuna.tsinghua.edu.cn"),
    ("阿里云", "https://mirrors.aliyun.com/pypi/simple", "mirrors.aliyun.com"),
    ("中科大", "https://pypi.mirrors.ustc.edu.cn/simple", "pypi.mirrors.ustc.edu.cn"),
    ("华为云", "https://mirrors.huaweicloud.com/repository/pypi/simple", "mirrors.huaweicloud.com"),
    ("官方PyPI", "https://pypi.org/simple", "pypi.org"),
]

# 关键库冒烟清单：能抓出 numpy/cv2/win32 这类“装上了但 DLL 加载失败”的问题。
# 只放核心功能必需且导入轻量的库；torch/demucs 是可选重包（应用有降级），不列入。
SMOKE_MODULES = [
    "numpy", "pkg_resources", "cv2", "mss", "flask", "pretty_midi", "mido",
    "pyautogui", "pydantic", "sounddevice", "soundfile", "pyaudiowpatch",
    "win32gui", "win32api", "win32process", "basic_pitch",
    "keyboard", "typer", "loguru", "rich",
    # 桌面版入口第一行就是 import tkinter；精简版 Python 没有 Tcl/Tk，
    # 必须在这里抓出来（pip 装不了 tkinter，只能换完整版 Python 重建 venv）。
    "tkinter",
]

# 可选组件：不阻塞主程序启动，缺了只让对应功能降级。
# 用 find_spec 探测（不 import torch，避免每次启动多等几秒）。
OPTIONAL_PACKAGES = {
    "demucs": "demucs>=4.0.1",
}

# ① 页「高级分离」的六轨推理组件（BS-Roformer-SW，通过 pymss 调用）。
# pymss 2.2+ 要求 Python 3.11+，项目是 3.10.11，所以锁 2.1.9。
ADVANCED_PACKAGES = {
    "pymss": "pymss==2.1.9",
}
# pymss 的 torch 依赖要求 setuptools>=77，而 basic-pitch 的 resampy 还依赖
# pkg_resources（setuptools>=81 已移除）——一起装 80.9.0 才能两边都满足。
ADVANCED_EXTRA = ["setuptools<81"]

# 高级分离只用 GPU。CUDA 12.6 官方支持 Maxwell(sm_52) ~ Hopper(sm_90)；
# RTX 50 系（Blackwell, sm_120）必须用 CUDA 12.8 版 torch，否则会报
# "CUDA kernel errors might be asynchronously reported" 且一条音轨都不产出。
# 阿里云镜像实测只有 ~0.1MB/s；上海交大镜像快一个数量级。
# 注意：镜像根目录是 PEP503 索引（按包名分子目录），pip -f 要指到 torch/、torchaudio/。
GPU_FLAVORS = {
    "cu126": {
        "torch": "2.7.1+cu126",
        "mirror": (
            "https://mirror.sjtu.edu.cn/pytorch-wheels/cu126/torch/",
            "https://mirror.sjtu.edu.cn/pytorch-wheels/cu126/torchaudio/",
        ),
        "fallback": (
            "https://download.pytorch.org/whl/cu126/torch/",
            "https://download.pytorch.org/whl/cu126/torchaudio/",
        ),
        "label": "CUDA 12.6（GTX/RTX 20~40 系）",
    },
    "cu128": {
        "torch": "2.7.1+cu128",
        # cu128 的国内镜像要么没有文件、要么只有 0.1MB/s；官方源实测快一个数量级，
        # 所以 cu128 优先官方，交大只作为官方被墙时的备份。
        "mirror": (
            "https://download.pytorch.org/whl/cu128/torch/",
            "https://download.pytorch.org/whl/cu128/torchaudio/",
        ),
        "fallback": (
            "https://mirror.sjtu.edu.cn/pytorch-wheels/cu128/torch/",
            "https://mirror.sjtu.edu.cn/pytorch-wheels/cu128/torchaudio/",
        ),
        "label": "CUDA 12.8（RTX 50 系 Blackwell，官方源）",
    },
}


def nvidia_gpu_name() -> str:
    """读显卡型号；没有 nvidia-smi / 没显卡返回空串。"""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return ""
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=8,
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
        if out.returncode != 0:
            return ""
        names = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
        return names[0] if names else ""
    except Exception:  # noqa: BLE001
        return ""


def pick_cuda_flavor(gpu_name: str) -> str:
    """RTX 50 系（Blackwell）必须 cu128；其余用兼容面更广的 cu126。"""
    if re.search(r"RTX\s*50\d0|B100|B200|GB200|Blackwell", gpu_name or "", re.I):
        return "cu128"
    return "cu126"


def torch_cuda_info() -> tuple[str, str]:
    """返回 (torch 版本, torch.version.cuda)；子进程读，避免旧模块缓存。"""
    code = ("import torch,sys;"
            "sys.stdout.write((torch.__version__ or '')+'|'+str(torch.version.cuda or ''))")
    try:
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=90,
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
        if out.returncode != 0:
            return "", ""
        ver, _, cuda = out.stdout.strip().partition("|")
        return ver, cuda
    except Exception:  # noqa: BLE001
        return "", ""


def run(cmd: list[str]) -> int:
    print("    $", " ".join(cmd), flush=True)
    return subprocess.call(cmd)


def smoke_imports() -> list[str]:
    """返回导入失败的模块名列表；空列表 = 环境健康。"""
    failed = []
    for name in SMOKE_MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name} ({type(exc).__name__})")
    return failed


def ensure_pip() -> bool:
    """确保 venv 里有 pip：部分拷贝/中断创建的 .venv 只有解释器没有 pip，
    此时所有镜像源都会报 "No module named pip"，用 ensurepip 就地修复。"""
    try:
        import pip  # noqa: F401
        return True
    except ModuleNotFoundError:
        print("[bootstrap] 虚拟环境缺少 pip，尝试用 ensurepip 修复…", flush=True)
        rc = subprocess.call([sys.executable, "-m", "ensurepip", "--upgrade"])
        if rc != 0:
            print("[bootstrap] ensurepip 修复失败，请删除 .venv 后重新双击 启动.bat。")
            return False
        return True


def pip_install(base_url: str, host: str, trusted: bool) -> int:
    cmd = [
        sys.executable, "-m", "pip", "install",
        "-r", str(REQUIREMENTS),
        "-i", base_url,
        "--timeout", "60",
        "--retries", "3",
        "--disable-pip-version-check",
    ]
    if trusted:
        cmd += ["--trusted-host", host]
    return run(cmd)


def optional_missing() -> list[str]:
    """返回还没装上的可选组件名（find_spec 探测，不触发 import）。"""
    import importlib.util

    return [name for name in OPTIONAL_PACKAGES if importlib.util.find_spec(name) is None]


def install_optional() -> int:
    """只补装可选组件（如 demucs），失败不影响主程序启动。"""
    missing = optional_missing()
    if not missing:
        print("[bootstrap] 可选组件已就绪：" + "、".join(OPTIONAL_PACKAGES))
        return 0

    specs = [OPTIONAL_PACKAGES[name] for name in missing]
    print("[bootstrap] 安装可选组件（" + "、".join(missing) + "）。")
    print("[bootstrap] 首次会连带下载 PyTorch，约 500MB~1GB，请耐心等待…")
    return install_specs(specs, "可选组件")


def install_advanced() -> int:
    """只补装「高级分离」组件 pymss（六轨模型推理），失败不影响普通转写。"""
    import importlib.util

    if importlib.util.find_spec("pymss") is not None:
        print("[bootstrap] 高级分离组件已就绪：pymss")
        return 0

    print("[bootstrap] 安装高级分离组件 pymss（BS-Roformer-SW 六轨分离）。")
    print("[bootstrap] 首次会连带下载 PyTorch，约 1GB，请耐心等待…")
    specs = [ADVANCED_PACKAGES["pymss"], *ADVANCED_EXTRA]
    return install_specs(specs, "高级分离组件")


def cuda_torch_ready() -> bool:
    """当前环境里的 torch 能不能用 CUDA。

    必须用子进程检查：本进程在安装前可能已经 import 过旧的 CPU 版 torch，
    装完新 torch 后内存里还是旧模块，直接 `torch.cuda.is_available()` 会误报 False。
    """
    code = "import torch,sys;sys.stdout.write('1' if torch.cuda.is_available() else '0')"
    try:
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=90,
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
        return out.returncode == 0 and out.stdout.strip() == "1"
    except Exception:  # noqa: BLE001
        return False


def cuda_version_tuple(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in (text or "").split(".") if p.isdigit())
    except ValueError:
        return ()


def cuda_flavor_ok(flavor: str, cuda_version: str) -> bool:
    """装好的 torch 的 CUDA 版本是否满足这张显卡。"""
    got = cuda_version_tuple(cuda_version)
    if flavor == "cu128":
        return got >= (12, 8)
    return bool(got) and got >= (12, 0)


def install_advanced_gpu() -> int:
    """安装与显卡匹配的 CUDA 版 PyTorch + pymss（高级分离只用 GPU）。

    RTX 50 系（Blackwell）必须 cu128；cu126 虽然能 `torch.cuda.is_available()`，
    但一跑模型就 CUDA kernel error、产出 0 个文件。其余显卡用 cu126。
    """
    import importlib.util

    pymss_ok = importlib.util.find_spec("pymss") is not None
    gpu = nvidia_gpu_name()
    flavor = pick_cuda_flavor(gpu)
    cfg = GPU_FLAVORS[flavor]
    cuda_ok = cuda_torch_ready()
    _, old_cuda = torch_cuda_info() if cuda_ok else ("", "")

    if pymss_ok and cuda_ok and cuda_flavor_ok(flavor, old_cuda):
        print(f"[bootstrap] 高级分离 GPU 组件已就绪（{gpu or 'CUDA'} / {cfg['label']}）")
        return 0

    if gpu:
        print(f"[bootstrap] 检测到显卡：{gpu} -> 安装 {cfg['label']} 版 PyTorch")
    if cuda_ok and not cuda_flavor_ok(flavor, old_cuda):
        print(f"[bootstrap] 当前 CUDA {old_cuda or '未知'} 与显卡不匹配，需要重装 {cfg['torch']}…")

    print(f"[bootstrap] 安装 CUDA 版 PyTorch + pymss（{cfg['torch']}，约 3.5GB）…")
    specs = [
        f"torch=={cfg['torch']}",
        f"torchaudio=={cfg['torch']}",
        ADVANCED_PACKAGES["pymss"],
        *ADVANCED_EXTRA,
    ]
    rc = install_specs(specs, "高级分离 GPU 组件", find_links=cfg["mirror"])
    if rc != 0:
        print("[bootstrap] 主下载源失败，改用备用源重试…")
        rc = install_specs(specs, "高级分离 GPU 组件", find_links=cfg["fallback"])
    if rc != 0:
        return rc
    if not cuda_torch_ready():
        print("[bootstrap] 组件装好了，但 torch.cuda.is_available() 仍为 False：")
        print("[bootstrap] 请确认 NVIDIA 驱动正常（nvidia-smi 能列出显卡）。")
        return 4
    _, new_cuda = torch_cuda_info()
    if not cuda_flavor_ok(flavor, new_cuda):
        print(f"[bootstrap] 已装 torch 的 CUDA 版本是 {new_cuda or '未知'}，仍不满足 {cfg['label']}。")
        print("[bootstrap] 请更新 NVIDIA 驱动后重试。")
        return 4
    print(f"[bootstrap] GPU 组件就绪：{cfg['torch']} / CUDA {new_cuda}")
    return 0


def install_specs(specs: list[str], label: str,
                  find_links: str | tuple[str, ...] | None = None) -> int:
    """按镜像顺序安装给定的 pip 规格，返回最后一个退出码。

    find_links: CUDA 版 torch 这类不在普通 PyPI 镜像里的 wheel，用 -f 指定源；
    可以是单个 URL 或一组 URL（torch/、torchaudio/ 各一个）。
    """
    links: list[str] = []
    if isinstance(find_links, str):
        links = [find_links]
    elif find_links:
        links = list(find_links)

    last_rc = 1
    for name, url, host in MIRRORS:
        print(f"[bootstrap] 使用镜像源：{name}")
        cmd = [
            sys.executable, "-m", "pip", "install", *specs,
            "-i", url, "--timeout", "60", "--retries", "2",
            # 强制进度条：GUI 里是管道，pip 默认会关掉，导致看起来一直没动静
            "--progress-bar", "on",
            "--disable-pip-version-check",
        ]
        for link in links:
            cmd += ["-f", link]
        last_rc = run(cmd)
        if last_rc != 0:
            print(f"[bootstrap] {name} 直连失败，带 trusted-host 重试一次…")
            last_rc = run(cmd + ["--trusted-host", host])
        if last_rc == 0:
            break
        print(f"[bootstrap] {name} 安装失败，切换下一镜像源…")

    if last_rc == 0:
        print(f"[bootstrap] {label}安装完成。")
    else:
        print(f"[bootstrap] {label}安装失败（不影响主程序，对应功能继续置灰）。")
    return last_rc



def main() -> int:
    # check 模式：只做关键库真实导入冒烟，不安装（供 launcher.ps1 判断 venv 健康度）
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        failed = smoke_imports()
        if failed:
            print("[bootstrap] 环境不完整：" + "、".join(failed))
            if any(f.startswith("tkinter") for f in failed):
                print("[bootstrap] tkinter 缺失：当前 Python 没装 Tcl/Tk（常见于精简版/商店版）。")
                print("[bootstrap] tkinter 不能用 pip 装；请关闭本窗口后重新双击 启动.bat，")
                print("[bootstrap] 启动器会自动备份 .venv，并用带 Tcl/Tk 的完整版 Python 重建。")
            return 1
        return 0

    # optional 模式：只补装可选组件，缺失/失败都不阻塞主程序
    if len(sys.argv) > 1 and sys.argv[1] == "optional":
        return install_optional()

    # advanced 模式：只补装「高级分离」六轨组件
    if len(sys.argv) > 1 and sys.argv[1] == "advanced":
        return install_advanced()

    # advanced-gpu 模式：CUDA 版 PyTorch + pymss（高级分离只用 GPU）
    if len(sys.argv) > 1 and sys.argv[1] == "advanced-gpu":
        return install_advanced_gpu()

    if not REQUIREMENTS.exists():
        print(f"[bootstrap] 找不到 {REQUIREMENTS.name}")
        return 1

    if not ensure_pip():
        return 1

    # 先升级 pip 自身（旧 pip 解析新 wheel 元数据会出问题）
    for label, url, host in MIRRORS:
        rc = run([sys.executable, "-m", "pip", "install", "--upgrade", "pip",
                  "-i", url, "--trusted-host", host, "--disable-pip-version-check"])
        if rc == 0:
            break
        print(f"[bootstrap] 从 {label} 升级 pip 失败，换源…")

    # 多源回退安装：每个源先直连，失败再带 trusted-host 重试一次
    last_rc = 1
    for label, url, host in MIRRORS:
        print(f"[bootstrap] 使用镜像源：{label}")
        last_rc = pip_install(url, host, trusted=False)
        if last_rc != 0:
            print(f"[bootstrap] {label} 直连失败，带 trusted-host 重试一次…")
            last_rc = pip_install(url, host, trusted=True)
        if last_rc == 0:
            break
        print(f"[bootstrap] {label} 安装失败，切换下一镜像源…")

    if last_rc != 0:
        print("[bootstrap] 所有镜像源均安装失败，请检查网络/代理后重试。")
        return 2

    print("[bootstrap] 依赖安装结束，正在做真实导入冒烟…")
    # 必须用“全新解释器子进程”复查：本进程启动于 pip 安装之前，像 pywin32
    # 这类靠 .pth 追加搜索路径（win32/、pywin32_system32）的包，在旧进程里
    # sys.path 已固化，会被误判 ModuleNotFoundError。重新拉起自己走 check 分支。
    rc = subprocess.call([sys.executable, str(Path(__file__).resolve()), "check"])
    if rc != 0:
        print("[bootstrap] 建议删除 .venv 目录后重新双击 启动.bat 完整重建。")
        return 3

    print("[bootstrap] 全部关键库导入正常，环境就绪。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
