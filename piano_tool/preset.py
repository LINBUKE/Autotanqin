"""用户预设：把 ②③④⑤ 的全部设置打包成一个 `.ini`，可导出/导入/自动沿用。

设计要点：
- **文件是真正的 INI**（用户要求的 `用户预设.ini`），用记事本也能改；
  段名对应功能模块（map / calibration / latency / play），
  结构复杂的内容（校准键位坐标、延迟样本库）以 JSON 字符串放进 `payload` 键。
- **两种导出**：
  - `with_song=False` → 只含设置（几十行，方便分享给别人）；
  - `with_song=True`  → 额外带一首 MIDI（base64 内嵌在 `[song]` 段）
    与 ④ 采样录下的每键音色（`[samples]` 段，见 `sample_bank.py`）。
- `interpolation=None`：INI 里 `%`、`$` 都是普通字符，不会被 configparser 当模板展开
  （校准数据里出现百分号是常事，不开这个会直接抛异常）。
- 读取一律「宁松不严」：缺段/缺键取默认、值类型不对就跳过，坏文件给一句人话错误。
"""

from __future__ import annotations

import base64
import configparser
import io
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# v2（2026-10-31）：新增 [samples] 段（每键采样 wav，base64）。
# 老版本读到这个段会原样保留、但不会应用，不影响它读其余设置。
PRESET_VERSION = 2
APP_TAG = "风物之诗琴自动化工具"
DEFAULT_NAME = "用户预设.ini"

# 预设里的段（顺序即写出顺序，读的时候按需取）
SECTION_ORDER = ("meta", "app", "instrument", "map", "calibration",
                 "latency", "play", "focus_target", "samples", "song")


class PresetError(ValueError):
    """预设文件坏了 / 不是本软件的预设。"""


# ---------------------------------------------------------------------------
# 值编解码
# ---------------------------------------------------------------------------
def pack_blob(obj: Any) -> str:
    """把 dict/list 压成一行 JSON（放进 INI 的某个键）。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def unpack_blob(text: Optional[str], default=None):
    """把 INI 里的 JSON 字符串还原；空/坏值返回 default。"""
    if not text or not str(text).strip():
        return default
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        logger.warning("预设里的 payload 解析失败，按空处理", exc_info=True)
        return default


def b64_of(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def bytes_of(text: Optional[str]) -> Optional[bytes]:
    if not text:
        return None
    try:
        return base64.b64decode(str(text).strip(), validate=False)
    except Exception:  # noqa: BLE001
        return None


def to_bool(value, default=False) -> bool:
    """INI 里布尔值可能是 1/0/true/false/yes/no/开/关。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on", "是", "开"):
        return True
    if s in ("0", "false", "no", "n", "off", "否", "关", ""):
        return False
    return default


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------
def dump_preset(state: Dict[str, Dict[str, Any]]) -> str:
    """state → INI 文本（带注释头，方便用户手改）。"""
    cp = configparser.ConfigParser(interpolation=None, allow_no_value=False)
    cp.optionxform = str            # 键名保持原样，别被转小写
    for section in SECTION_ORDER:
        if section in state and state[section] is not None:
            cp[section] = {str(k): str(v) for k, v in state[section].items()}
    # 把没在 SECTION_ORDER 里的段也写出来（向前兼容）
    for section, body in state.items():
        if section not in SECTION_ORDER and body:
            cp[section] = {str(k): str(v) for k, v in body.items()}

    buf = io.StringIO()
    buf.write(f"; {APP_TAG} · 用户预设\n")
    buf.write("; 可直接用记事本打开修改；导入时缺的项会沿用当前界面上的值。\n")
    buf.write("; [song] 段是可选的：带 MIDI 的「全量预设」才有。\n\n")
    cp.write(buf)
    return buf.getvalue()


def parse_preset(text: str) -> Dict[str, Dict[str, str]]:
    """INI 文本 → state（全是字符串，取值时再转换）。"""
    cp = configparser.ConfigParser(interpolation=None, allow_no_value=False)
    cp.optionxform = str
    try:
        cp.read_string(text)
    except Exception as exc:  # noqa: BLE001
        raise PresetError(f"预设文件格式不对：{exc}") from exc

    state: Dict[str, Dict[str, str]] = {}
    for section in cp.sections():
        state[section] = dict(cp.items(section))

    # 认不出来就别误导用户（别人随便一个 ini 也选进来时会走到这里）
    app = state.get("meta", {}).get("app", "")
    if not state:
        raise PresetError("预设文件是空的")
    if app and APP_TAG not in app:
        raise PresetError(f"这不是本软件的预设（app={app}）")
    if not app and not ({"map", "play", "calibration"} & set(state)):
        raise PresetError("预设里没有可用的设置段（map / play / calibration）")
    return state


def build_meta(with_song: bool) -> Dict[str, str]:
    return {
        "app": APP_TAG,
        "version": str(PRESET_VERSION),
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "with_song": "1" if with_song else "0",
    }


# ---------------------------------------------------------------------------
# 文件读写
# ---------------------------------------------------------------------------
def write_preset(path, state: Dict[str, Dict[str, Any]]) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dump_preset(state), encoding="utf-8")
    return p


def read_preset(path) -> Dict[str, Dict[str, str]]:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # 有人用记事本存成 GBK 了，也认
        text = p.read_text(encoding="gbk", errors="replace")
    except OSError as exc:
        raise PresetError(f"读不到文件：{exc}") from exc
    return parse_preset(text)
