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
]

# 可选组件：不阻塞主程序启动，缺了只让对应功能降级。
# 用 find_spec 探测（不 import torch，避免每次启动多等几秒）。
OPTIONAL_PACKAGES = {
    "demucs": "demucs>=4.0.1",
}


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

    last_rc = 1
    for label, url, host in MIRRORS:
        print(f"[bootstrap] 使用镜像源：{label}")
        cmd = [
            sys.executable, "-m", "pip", "install", *specs,
            "-i", url, "--timeout", "60", "--retries", "2",
            # 强制进度条：GUI 里是管道，pip 默认会关掉，导致看起来一直没动静
            "--progress-bar", "on",
            "--disable-pip-version-check",
        ]
        last_rc = run(cmd)
        if last_rc != 0:
            print(f"[bootstrap] {label} 直连失败，带 trusted-host 重试一次…")
            last_rc = run(cmd + ["--trusted-host", host])
        if last_rc == 0:
            break
        print(f"[bootstrap] {label} 安装失败，切换下一镜像源…")

    if last_rc == 0:
        print("[bootstrap] 可选组件安装完成。")
    else:
        print("[bootstrap] 可选组件安装失败（不影响主程序，人声分离继续置灰）。")
    return last_rc


def main() -> int:
    # check 模式：只做关键库真实导入冒烟，不安装（供 launcher.ps1 判断 venv 健康度）
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        failed = smoke_imports()
        if failed:
            print("[bootstrap] 环境不完整：" + "、".join(failed))
            return 1
        return 0

    # optional 模式：只补装可选组件，缺失/失败都不阻塞主程序
    if len(sys.argv) > 1 and sys.argv[1] == "optional":
        return install_optional()

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
