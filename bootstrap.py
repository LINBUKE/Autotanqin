"""风物之诗琴自动化工具 · 依赖自动引导脚本

由 launcher.ps1（或手动）用一个版本兼容的基础解释器调用，负责：
  1. 创建 / 修复项目 .venv 虚拟环境
     - 已有的 .venv 会真实执行检查：解释器损坏、或是由不兼容的 Python
       版本（如 3.12/3.13、或从别的电脑拷过来）创建的 → 自动删除重建
  2. 离线检测依赖是否满足 requirements.txt（不触发 tensorflow 等重型加载）
  3. 依赖缺失时自动 pip install；某源下载失败 / 超时自动换下一个：
     清华 TUNA → 阿里云 → 中科大 USTC → 华为云 → PyPI 官方
  4. 用 venv 内解释器启动目标程序

任何失败都会把完整堆栈写入 data/bootstrap_error.log，便于截图反馈。

用法：
    python bootstrap.py            # 默认启动桌面版 GUI
    python bootstrap.py desktop    # 桌面版 GUI
    python bootstrap.py web        # 网页控制台 (app.py)
    python bootstrap.py menu       # 命令行菜单 (run.py)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
ERROR_LOG = ROOT / "data" / "bootstrap_error.log"

# basic-pitch 0.4.0 的兼容窗口：3.9 <= Python < 3.12
MIN_PYTHON = (3, 9)
MAX_PYTHON_EXCLUSIVE = (3, 12)

# pip 镜像源：国内源在前（快），官方源兜底。
PIP_INDEXES = [
    ("清华 TUNA", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    ("阿里云", "https://mirrors.aliyun.com/pypi/simple/"),
    ("中科大 USTC", "https://mirrors.ustc.edu.cn/pypi/simple"),
    ("华为云", "https://mirrors.huaweicloud.com/repository/pypi/simple"),
    ("PyPI 官方", "https://pypi.org/simple"),
]

# requirements.txt 中的有效依赖行（去掉注释/空行），作为离线版本校验依据。
REQUIREMENT_LINES = [
    line
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
    if (line := raw.split("#", 1)[0].strip())
]

# 在 venv 中执行：逐包比对“已安装版本”是否满足 requirements 的版本约束。
# 纯离线、不触发重型包导入；输出空串=全部满足，NEED_INSTALL=基础工具缺失。
_CHECK_CODE = (
    "try:\n"
    "    import importlib.metadata as md\n"
    "    from packaging.requirements import Requirement\n"
    "except Exception:\n"
    "    print('NEED_INSTALL')\n"
    "else:\n"
    "    bad=[]\n"
    "    for line in %r:\n"
    "        r=Requirement(line)\n"
    "        try:\n"
    "            v=md.version(r.name)\n"
    "        except md.PackageNotFoundError:\n"
    "            bad.append(r.name+':missing'); continue\n"
    "        if not r.specifier.contains(v, prereleases=True):\n"
    "            bad.append(r.name+' '+v+' !'+str(r.specifier))\n"
    "    print(';'.join(bad))\n"
) % REQUIREMENT_LINES

# 启动目标：参数 -> 入口脚本
TARGETS = {
    "desktop": ROOT / "desktop_app.py",
    "web": ROOT / "app.py",
    "menu": ROOT / "run.py",
}
TARGET_NAMES = {
    "desktop": "桌面版 GUI",
    "web": "网页控制台",
    "menu": "启动菜单",
}


def info(msg: str) -> None:
    print(f"[bootstrap] {msg}", flush=True)


def version_in_window(ver: tuple[int, int]) -> bool:
    return MIN_PYTHON <= ver < MAX_PYTHON_EXCLUSIVE


def venv_python() -> Path:
    """返回当前平台 venv 内解释器路径。"""
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def get_python_version(py: Path) -> tuple[int, int, int] | None:
    """真实执行解释器取版本；失败/超时返回 None。"""
    try:
        out = subprocess.check_output(
            [str(py), "-c", "import sys;print('%d.%d.%d'%sys.version_info[:3])"],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        ).strip()
    except Exception:
        return None
    parts = out.split(".")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return tuple(int(p) for p in parts)  # type: ignore[return-value]
    return None


def _venv_cfg_command_line() -> str:
    """读取 pyvenv.cfg 中 'command = ...' 行（venv 创建时的原始命令）；没有则空串。"""
    cfg = VENV_DIR / "pyvenv.cfg"
    if not cfg.exists():
        return ""
    try:
        for raw in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
            if raw.strip().lower().startswith("command"):
                return raw.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def venv_created_in_place() -> bool:
    """判断 .venv 是否在当前目录原生创建。

    pyvenv.cfg 的 command 行记录了创建时的目标路径；若当前路径不在其中，
    说明这个 venv 是从别的目录/电脑拷过来的（里面的绝对路径与二进制
    依赖会失效），必须重建。没有 command 行（极旧的 venv）也视为不可信。
    """
    cmdline = _venv_cfg_command_line()
    if not cmdline:
        return False
    return str(VENV_DIR.resolve()).lower() in cmdline.lower()


# 复用现有 venv 前的“轻量导入冒烟”：真正 import 关键运行时库，
# 能抓出“元数据在但 DLL 加载失败”这类损坏（如拷贝导致的 onnxruntime 损坏）。
# onnxruntime 仅在已安装时才检查；不导入 basic_pitch/tensorflow（太重）。
_LIGHT_IMPORT_CODE = (
    "import importlib.metadata as md\n"
    "mods=['numpy','cv2','mss','sounddevice','pyautogui','keyboard',"
    "'flask','pydantic','typer','rich','pretty_midi']\n"
    "try:\n"
    "    md.version('onnxruntime'); mods.append('onnxruntime')\n"
    "except Exception: pass\n"
    "bad=[]\n"
    "for m in mods:\n"
    "    try: __import__(m)\n"
    "    except Exception: bad.append(m)\n"
    "print(';'.join(bad))\n"
)


def light_import_failures(py: Path) -> list[str]:
    """执行导入冒烟，返回导入失败的模块名（空列表=全部正常）。"""
    try:
        out = subprocess.check_output(
            [str(py), "-c", _LIGHT_IMPORT_CODE],
            text=True, encoding="utf-8", errors="replace", timeout=60,
        ).strip()
    except Exception:
        return ["<冒烟脚本无法执行>"]
    return out.split(";") if out else []


# 重建期间旧 venv 的备份路径（新环境验证通过后由 main 清理）
_VENV_BACKUP: Path | None = None


def _rebuild_venv(reason: str) -> Path:
    """安全重建 .venv：旧目录先改名备份，新环境失败则回滚。"""
    global _VENV_BACKUP

    # 若本脚本正运行在待重建的 venv 内，无法自替换 → 明确提示。
    if VENV_DIR.exists() and Path(sys.executable).resolve().is_relative_to(VENV_DIR.resolve()):
        raise RuntimeError(
            "bootstrap 正运行在待重建的 .venv 内部，无法自替换。"
            "请关闭后双击「启动.bat」，由外部解释器完成重建。"
        )

    info("现有 .venv 不可用（" + reason + "），自动重建（旧环境先备份）…")

    # 重建用的基础解释器必须是兼容版本（launcher.ps1 已保证；手动执行时兜底）。
    if not version_in_window(sys.version_info[:2]):
        raise RuntimeError(
            f"当前 Python {sys.version_info.major}.{sys.version_info.minor} 不在兼容范围"
            "（仅支持 3.9 ~ 3.11）。请重新双击「启动.bat」，会自动安装兼容版本。"
        )

    backup: Path | None = None
    if VENV_DIR.exists():
        import time as _time

        backup = VENV_DIR.with_name(".venv_backup_" + _time.strftime("%Y%m%d_%H%M%S"))
        os.rename(str(VENV_DIR), str(backup))
        _VENV_BACKUP = backup

    info(f"使用 Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} 创建虚拟环境…")
    try:
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
        py = venv_python()
        if not py.exists() or get_python_version(py) is None:
            raise RuntimeError("新虚拟环境解释器不可用 " + str(py))
    except Exception:
        # 创建失败：删掉半成品，尽量把备份恢复回 .venv
        shutil.rmtree(VENV_DIR, ignore_errors=True)
        if backup is not None and backup.exists():
            try:
                os.rename(str(backup), str(VENV_DIR))
                _VENV_BACKUP = None
                info("已恢复原来的 .venv。")
            except Exception:
                pass
        raise
    return py


def ensure_venv() -> Path:
    """确保 .venv 原生、健康且版本兼容，返回 venv python 路径。

    复用现有 venv 的三个条件（任一不满足则安全重建）：
      1. 解释器能真实执行，且 Python 版本在兼容窗口内
      2. pyvenv.cfg 显示它是在当前目录创建的（非跨目录/跨机器拷贝）
      3. 关键运行时库能真正导入（无 DLL 损坏）
    """
    py = venv_python()

    if py.exists():
        v = get_python_version(py)
        if v is None:
            return _rebuild_venv("解释器无法执行，可能已损坏")
        if not version_in_window(v[:2]):
            return _rebuild_venv(f"Python {v[0]}.{v[1]}.{v[2]} 版本不兼容（仅支持 3.9~3.11）")
        if not venv_created_in_place():
            return _rebuild_venv("该环境创建于其他目录/电脑（拷贝的 venv，内部路径会失效）")
        bad_imports = light_import_failures(py)
        if bad_imports:
            return _rebuild_venv("关键库导入失败：" + ", ".join(bad_imports))
        return py

    # .venv 不存在：直接创建（无需备份）
    if not version_in_window(sys.version_info[:2]):
        raise RuntimeError(
            f"当前 Python {sys.version_info.major}.{sys.version_info.minor} 不在兼容范围"
            "（仅支持 3.9 ~ 3.11）。请重新双击「启动.bat」，会自动安装兼容版本。"
        )
    info(f"使用 Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} 创建虚拟环境…")
    subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
    if not py.exists() or get_python_version(py) is None:
        raise RuntimeError("虚拟环境创建失败：解释器不可用 " + str(py))
    return py


def cleanup_backup() -> None:
    """新环境依赖验证通过后，删除旧 venv 备份，释放磁盘空间。"""
    global _VENV_BACKUP
    if _VENV_BACKUP and _VENV_BACKUP.exists():
        shutil.rmtree(_VENV_BACKUP, ignore_errors=True)
        info(f"已清理旧环境备份：{_VENV_BACKUP.name}")
    _VENV_BACKUP = None


def dependency_problems(py: Path) -> str:
    """离线检查依赖是否满足 requirements.txt，返回问题描述（空串=全部满足）。"""
    try:
        out = subprocess.check_output(
            [str(py), "-c", _CHECK_CODE],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        ).strip()
    except Exception:
        return "依赖检查无法执行，需要安装/修复依赖"
    if out == "NEED_INSTALL":
        return "基础校验工具缺失，需要安装依赖"
    return out


def _pip_install(py: Path, args: list[str], index_url: str) -> int:
    """用指定镜像源执行 pip，返回退出码。"""
    cmd = [
        str(py), "-m", "pip", "install",
        "--disable-pip-version-check",
        "--no-input",
        "--timeout", "60",
        "--retries", "2",
        "-i", index_url,
        *args,
    ]
    return subprocess.call(cmd)


def pip_install_with_fallback(py: Path, args: list[str], what: str) -> bool:
    """依次尝试各镜像源执行 pip 安装；任一成功即返回 True。"""
    for name, url in PIP_INDEXES:
        info(f"使用 {name} 源{what} …")
        try:
            code = _pip_install(py, args, url)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # 源本身异常（极少见）→ 换源
            info(f"{name} 源出现异常：{exc}，换下一个源")
            continue
        if code == 0:
            return True
        info(f"{name} 源安装失败（退出码 {code}），自动切换下一个源…")
    return False


def ensure_dependencies(py: Path) -> None:
    """确保所有依赖满足 requirements；缺失或版本不符则多源回退安装。"""
    problems = dependency_problems(py)
    if not problems:
        info("依赖已齐全。")
        return

    info("依赖检查未通过：" + problems.replace(";", "；"))
    info("开始安装 requirements.txt（首次安装可能需要几分钟）…")

    # 先升级打包工具链（失败不致命，继续装 requirements）。
    # setuptools 必须 <81：81+ 移除了 pkg_resources，而 basic-pitch 依赖的
    # resampy 仍在使用它。
    pip_install_with_fallback(
        py, ["-U", "pip", "wheel", "setuptools<81"], "升级基础打包工具"
    )

    if not pip_install_with_fallback(py, ["-r", str(REQUIREMENTS)], "安装项目依赖"):
        raise RuntimeError(
            "所有镜像源均安装失败。请检查网络连接后重试，"
            "或手动执行： pip install -r requirements.txt"
        )

    still_bad = dependency_problems(py)
    if still_bad:
        raise RuntimeError("安装完成后依赖仍不满足：" + still_bad.replace(";", "；"))
    info("全部依赖安装完成。")


def launch(py: Path, target_name: str) -> None:
    script = TARGETS[target_name]
    if not script.exists():
        raise RuntimeError(f"入口文件不存在：{script}")
    info(f"启动{TARGET_NAMES[target_name]}…")
    # 用 venv 解释器替换当前进程；Windows 上 os.execv 启动新进程后本进程立即退出。
    os.execv(str(py), [str(py), str(script)])


def write_error_log() -> None:
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        ERROR_LOG.write_text(traceback.format_exc(), encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    target = sys.argv[1].strip().lower() if len(sys.argv) > 1 else "desktop"
    if target not in TARGETS:
        info(f"未知目标 “{target}”，可选：{', '.join(TARGETS)}")
        return 2

    print("=" * 60)
    print("  风物之诗琴自动化工具 · 依赖自动检测 / 安装")
    print("=" * 60)
    try:
        py = ensure_venv()
        ensure_dependencies(py)
        # 到这里新环境已创建并通过全部依赖校验，才删除旧 venv 备份（可安全释放空间）
        cleanup_backup()
        launch(py, target)
    except KeyboardInterrupt:
        info("已取消。")
        return 130
    except Exception as exc:
        write_error_log()
        print(f"[bootstrap][错误] {exc}", file=sys.stderr)
        print(f"详细日志：{ERROR_LOG}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
