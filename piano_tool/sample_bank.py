"""④ 采样顺手录下的每键音色（`data/key_samples/*.wav`）的打包 / 还原。

用途：编辑器「模拟音」全靠这些 wav 还原真实琴声。它们原来只躺在 `data/` 里，
换机器、或者换了一份新的发布包，就得重新做一次 ④ 采样（要对着游戏一个键一个键录，
几十秒到一分钟）。现在它们随「导出全量预设」一起走：写进 INI 的 `[samples]` 段
（base64），导入即恢复，不用再录。

**为什么塞 INI 而不是单独 zip**：
- 用户要的是「一份文件带走所有设置」，`[song]` 段（MIDI 也是 base64）已经是这个思路；
- 少一层「解压到哪个目录」的解释成本，也少一类「放错位置」的故障。

代价是体积 ×1.33（base64），所以有 `MAX_TOTAL_BYTES` 兜底：超了宁可不带，
也不要让预设变成几十 MB、记事本打不开的怪物。

段格式（`[samples]`）::

    count = 21
    bytes = 1560000
    samplerate = 44100
    sampled_at = 2026-10-31T12:31:05
    index_b64 = <base64 of index.json>
    k_A = <base64 wav>
    k_S = ...
"""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)

# 键名前缀：避免和段里的 count/bytes/samplerate 这些元信息键重名，
# 也让「哪些键是波形」一目了然（不是靠猜哪个键像元数据）。
KEY_PREFIX = "k_"

# 打包上限（原始字节）。21 键 × 约 75KB ≈ 1.6MB，正常情况下远远够用。
MAX_TOTAL_BYTES = 25 * 1024 * 1024

_NAME_RE = re.compile(r"[^A-Za-z0-9_]")


def safe_name(name: str) -> str:
    """只留字母数字下划线：既是文件名白名单，也挡住 `../` 这类路径穿越。"""
    return _NAME_RE.sub("", str(name or ""))


def collect(sample_dir) -> Dict[str, object]:
    """读目录，返回 `{"files": {键: 字节}, "index": 字节|None, "total": 原始总字节}`。"""
    d = Path(sample_dir)
    files: Dict[str, bytes] = {}
    total = 0
    if d.is_dir():
        for p in sorted(d.glob("*.wav")):
            name = safe_name(p.stem)
            if not name:
                continue
            try:
                data = p.read_bytes()
            except OSError:
                logger.warning("读采样失败：%s", p, exc_info=True)
                continue
            if not data:
                continue
            files[name] = data
            total += len(data)
    index = None
    idx = d / "index.json"
    if idx.is_file():
        try:
            index = idx.read_bytes()
        except OSError:
            index = None
    return {"files": files, "index": index, "total": total}


def build_section(sample_dir, max_bytes: int = MAX_TOTAL_BYTES
                  ) -> Tuple[dict, int, int, str]:
    """把目录打成 INI 段。

    返回 `(section, 键数, 原始字节, note)`；`note` 是给日志/弹窗看的一句话。
    没有采样、或体积超限时 `section` 是空 dict（导出流程按「没有这一段」处理）。
    """
    got = collect(sample_dir)
    files: Dict[str, bytes] = got["files"]           # type: ignore[assignment]
    total = int(got["total"])                        # type: ignore[arg-type]
    n = len(files)
    if not n:
        return {}, 0, 0, "这次没有每键采样可带（要先做一次 ④ 采样）"
    if total > max_bytes:
        return {}, n, total, (
            f"每键采样共 {total / 1048576:.1f} MB，超过 {max_bytes / 1048576:.0f} MB 上限，"
            "这次没打包（它会撑大预设文件、记事本也打不开）")

    sec: Dict[str, str] = {"count": str(n), "bytes": str(total)}
    index: bytes | None = got["index"]               # type: ignore[assignment]
    meta: dict = {}
    if index:
        sec["index_b64"] = base64.b64encode(index).decode("ascii")
        try:
            meta = json.loads(index.decode("utf-8"))
        except Exception:  # noqa: BLE001  索引坏了不影响波形
            meta = {}
    try:
        sr = int(meta.get("samplerate") or 0)
    except (TypeError, ValueError):
        sr = 0
    if sr:
        sec["samplerate"] = str(sr)
    if meta.get("sampled_at"):
        sec["sampled_at"] = str(meta["sampled_at"])

    for name, data in files.items():
        sec[KEY_PREFIX + name] = base64.b64encode(data).decode("ascii")
    return sec, n, total, f"每键采样 {n} 个（{total / 1048576:.1f} MB）"


def restore(section, sample_dir) -> Tuple[int, List[str]]:
    """把 `[samples]` 段写回 `key_samples/`，返回 `(恢复的键数, 坏掉的键)`。"""
    d = Path(sample_dir)
    if not section:
        return 0, []
    d.mkdir(parents=True, exist_ok=True)
    got = 0
    bad: List[str] = []
    for key, val in section.items():
        name = safe_name(key[len(KEY_PREFIX):]) if str(key).startswith(KEY_PREFIX) else ""
        if not name or not val:
            continue
        try:
            data = base64.b64decode(str(val).strip(), validate=False)
        except Exception:  # noqa: BLE001
            bad.append(name)
            continue
        # RIFF 头校验：防止把半截/损坏的数据写成 wav，之后 decodeAudioData 报莫名其妙的错
        if not data.startswith(b"RIFF"):
            bad.append(name)
            continue
        try:
            (d / f"{name}.wav").write_bytes(data)
            got += 1
        except OSError:
            logger.warning("写采样失败：%s", name, exc_info=True)
            bad.append(name)

    try:
        raw = section.get("index_b64")
        if raw:
            (d / "index.json").write_bytes(base64.b64decode(str(raw)))
        elif got:
            keys = sorted(p.stem for p in d.glob("*.wav"))
            (d / "index.json").write_text(json.dumps({
                "sampled_at": datetime.now().isoformat(timespec="seconds"),
                "samplerate": int(section.get("samplerate") or 0),
                "keys": keys,
                "count": len(keys),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001  索引只是元信息，写不上不影响试听
        logger.warning("写 key_samples/index.json 失败", exc_info=True)
    return got, bad


def describe(sample_dir) -> str:
    """一句话摘要（给界面提示用）。"""
    got = collect(sample_dir)
    n = len(got["files"])                            # type: ignore[arg-type]
    if not n:
        return "还没有每键采样"
    total = int(got["total"])                        # type: ignore[arg-type]
    at = ""
    index = got["index"]                             # type: ignore[assignment]
    if index:
        try:
            at = str(json.loads(index.decode("utf-8")).get("sampled_at") or "")
        except Exception:  # noqa: BLE001
            at = ""
    return f"{n} 键 · {total / 1048576:.1f} MB" + (f" · 采于 {at.replace('T', ' ')}" if at else "")
