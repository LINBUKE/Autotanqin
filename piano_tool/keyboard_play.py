"""③ 「键盘映射对应」模式 —— 直接用键盘弹奏

③ 四点校准页上勾不勾「原神键位优化」决定走哪条路（**默认不勾 = 以前的方案**）：

    老方案：截屏 → 拖 4 个角点 → 透视算出琴键的屏幕坐标 → 播放时鼠标点击。
    原神键位优化（勾上才走）：直接把键盘字符当成琴键，按当前乐器的键位表来：

        风物之诗琴（21 键）  首席女高音（14 键，只有上两排发声）
        底行 Z~M = C4~B4     下排 A~J = C4~B4
        中行 A~J = C5~B5     上排 Q~U = C5~B5
        顶行 Q~U = C6~B6

       （低音在左、高音在右，和琴键从左到右一致。）

当前乐器由 `piano_tool.instruments.active()` 给出（② 页选乐器时切它）。

不需要截屏、不需要四点校准，换电脑/换键盘/换分辨率也不用重做；
播放时发送键盘按键事件，而不是鼠标点击，于是「可以用键盘弹奏」。

本模块只做纯逻辑 + 键盘输入，不碰鼠标坐标，也不修改原有的
「屏幕校准 + 鼠标点击」链路（那一套逻辑完全在 player.py 里）。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .instruments import active
from .models import (
    INPUT_MODE_KEYBOARD,
    INPUT_MODE_SCREEN,
    Calibration,
    KeyCoordinate,
)
from .waits import sleep_until

DEFAULT_HOLD_DURATION = 0.4   # 与编辑器 NOTE_DUR_DEFAULT 对齐：时值为空时按 0.4s 按住

logger = logging.getLogger("piano_tool.keyboard_play")

__all__ = [
    "INPUT_MODE_KEYBOARD",
    "INPUT_MODE_SCREEN",
    "ROW_SUMMARY",
    "keyboard_key_for_midi",
    "build_keyboard_calibration",
    "is_keyboard_calibration",
    "is_keyboard_mode",
    "normalize_key",
    "tap_key",
    "build_keyboard_schedule",
    "play_keyboard",
]

# 三行键盘 → 音区（与 21 白键从左到右的顺序一致）
ROW_SUMMARY: Dict[str, str] = {
    "bottom": "Z X C V B N M  →  C4(60) ~ B4(71)",
    "middle": "A S D F G H J  →  C5(72) ~ B5(83)",
    "top": "Q W E R T Y U  →  C6(84) ~ B6(95)",
}

ROW_LABELS: Tuple[str, ...] = (ROW_SUMMARY["bottom"], ROW_SUMMARY["middle"], ROW_SUMMARY["top"])


def keyboard_key_for_midi(midi) -> Optional[str]:
    """MIDI 音高 → 键盘字符；不在当前乐器的音域内返回 None。

    当前乐器不同，同一个音高对应的字母也不同（风物之诗琴 C5 是 A，
    首席女高音 C5 是 Q），所以这里跟着 `instruments.active()` 走。
    """
    return active().key_by_midi.get(int(midi))


def build_keyboard_calibration() -> Calibration:
    """生成「键盘映射」校准配置（按当前乐器的键位表）。

    键盘模式不关心屏幕坐标，所以坐标一律填 0 —— 播放/采样都走按键路径，
    不会去点 (0,0)。四角给一组占位值，保证 Corners 字段非空，
    这样任何读取 corners 的旧代码都不会拿到脏数据。
    """
    keys = [
        KeyCoordinate(
            key=str(k["key"]),
            midi=int(k["midi"]),
            name=str(k["name"]),
            x=0.0,
            y=0.0,
        )
        for k in active().keys
    ]
    return Calibration(
        corners=[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]],
        keys=keys,
        image_size=[1, 1],
        input_mode=INPUT_MODE_KEYBOARD,
    )


def is_keyboard_calibration(cal) -> bool:
    """这份校准是不是「键盘映射」方式。老文件没有 input_mode → 判定为 screen。"""
    return getattr(cal, "input_mode", INPUT_MODE_SCREEN) == INPUT_MODE_KEYBOARD


def is_keyboard_mode(mode) -> bool:
    """字符串形式的弹奏方式是否为键盘映射。"""
    return str(mode or INPUT_MODE_SCREEN) == INPUT_MODE_KEYBOARD


def normalize_key(key) -> str:
    """把 'Z' / ' z ' 规范化成 pyautogui 最稳的小写 'z'。

    pyautogui 直接发送单字符时会映射成对应字母键（无 Shift 时输入小写），
    传小写最稳，不会因为上一秒还按着 Shift 就变成大写。
    """
    return str(key).strip().lower()


def _send_key(key, down: bool, upper: bool = False) -> None:
    """按/抬一个键。upper=True 时先按一下 Shift，让浏览器收到大写字母。

    优先走 piano_tool.inject（SendInput + 真实扫描码）：一部分游戏走 DirectInput /
    RawInput，只认带扫描码的按键事件；而 pyautogui 的老接口 keybd_event 扫描码传 0，
    于是出现「浏览器能弹、切到游戏没反应」。inject 不可用或发送失败时回退 pyautogui，
    保证浏览器场景与旧机器行为完全不变。
    """
    from . import inject

    ch = normalize_key(key)
    if inject.send_key(ch, down, upper):
        return

    import pyautogui

    _pause = False
    if upper:
        if down:
            pyautogui.keyDown("shift", _pause=_pause)
            pyautogui.keyDown(ch, _pause=_pause)
        else:
            pyautogui.keyUp(ch, _pause=_pause)
            pyautogui.keyUp("shift", _pause=_pause)
    else:
        if down:
            pyautogui.keyDown(ch, _pause=_pause)
        else:
            pyautogui.keyUp(ch, _pause=_pause)


def tap_key(key, upper: bool = False, dry_run: bool = False,
            hold_ms: float = 0.0) -> None:
    """点一下某个键（不按住），供「🎯 自检」和「④ 延迟采样」用。

    hold_ms: 按下与抬起之间等多久（毫秒）。**键盘模式采样必须给**——
    游戏/模拟器对连按有去抖，`down` 紧跟 `up`（0ms）常常被当成「没按」，
    于是某些键采不到声音。鼠标点击天然带按下时长，所以以前只在鼠标模式下正常。
    """
    if dry_run:
        return
    _send_key(key, True, upper)
    if hold_ms and hold_ms > 0:
        time.sleep(float(hold_ms) / 1000.0)
    _send_key(key, False, upper)


# ---------------------------------------------------------------------------
# 播放：把音符时间线排成 (时刻, down/up, event)
# ---------------------------------------------------------------------------
def build_keyboard_schedule(
    events_sorted: List,
    base_lead: float,
    latency_lead: Optional[Dict[str, float]],
    speed: float,
    start_time: float,
    hold_min: float,
    delay_ms: int,
    hold: bool,
) -> List[Tuple[float, str, object, float]]:
    """同 player.build_hold_schedule，只是不返回坐标（键盘模式用不上）。

    latency_lead 已停用（保留参数兼容）：逐键提前量会改变音与音的相对时间，
    同一份谱子会错拍/不和谐；④ 的采样实际是给编辑器采集每键音色。
    """
    sched = []
    for e in events_sorted:
        # 逐键延迟补偿已停用：只用统一提前量
        lead = base_lead
        press_t = (e.time - start_time) / speed - lead
        dur = float(e.duration or 0.0)
        # 与编辑器一致：时值为空/0 时给默认 0.4s，否则按住会退化成 30ms 短敲
        if hold and dur <= 0:
            dur = DEFAULT_HOLD_DURATION
        if hold and dur > 0:
            release_t = (e.time - start_time + dur) / speed
            if release_t < press_t + hold_min:
                release_t = press_t + hold_min
        else:
            release_t = press_t + hold_min + (delay_ms / 1000.0 if delay_ms else 0.0)
        if press_t < 0:
            press_t = 0.0
        sched.append((press_t, "down", e, press_t))
        sched.append((release_t, "up", e, release_t))
    sched.sort(key=lambda a: a[0])
    return sched


def play_keyboard(
    events: List,
    speed: float = 1.0,
    delay_ms: int = 20,
    dry_run: bool = False,
    stop_check: Optional[callable] = None,
    latency_comp_ms: float = 0.0,
    latency_lead: Optional[Dict[str, float]] = None,
    hold: bool = False,
    hold_min_ms: float = 30.0,
    upper: bool = False,
    focus_check: Optional[callable] = None,
    on_focus_lost: str = "wait",
    status: Optional[callable] = None,
) -> List[float]:
    """按时间轴用键盘按键弹奏。返回时间偏差列表（实际 - 计划，秒）。

    与 player.play 的鼠标版参数一一对应，因此 ⑤ 播放页的倍速/提前量/
    按住时值/停止 在键盘模式下行为一致。

    焦点守卫（可选）：传入 focus_check（返回 (是否在目标窗口, 当前窗口标题)）
    就能解决「焦点跳到浏览器后按键仍被乱发」的问题 —— 注入按键只对前台窗口生效，
    焦点不在目标窗口时按 on_focus_lost 处理：

        wait（默认）抬起所有按住的键并暂停等待，切回目标窗口自动接着弹（时间轴整体顺延）
        skip           静音跳过失焦期间的事件，不停曲
        stop           立刻停止

    focus_check=None（默认）表示完全不检查，行为和以前一模一样。
    """
    from rich.progress import Progress

    hold_min = max(hold_min_ms / 1000.0, 0.0)
    events_sorted = sorted(events, key=lambda e: e.time)
    sched = build_keyboard_schedule(
        events_sorted,
        latency_comp_ms / 1000.0,
        latency_lead,
        speed,
        0.0,
        hold_min,
        delay_ms,
        hold,
    )
    deviations: List[float] = []
    down_keys = set()
    key_owner: Dict[str, object] = {}   # 键 -> 当前按下的那个音符事件（防止旧 up 掐断新长音）
    stopped = False
    lost_mode = str(on_focus_lost or "wait").lower()
    if lost_mode not in ("wait", "skip", "stop"):
        lost_mode = "wait"

    def _say(msg: str) -> None:
        if status is not None:
            try:
                status(msg)
            except Exception:  # noqa: BLE001  状态回传失败不能影响演奏
                logger.debug("status callback failed", exc_info=True)

    def _release_all() -> None:
        """抬起所有还按着的键 —— 失焦暂停/异常/收尾都要走一遍，防止卡键。"""
        for k in list(down_keys):
            if not dry_run:
                _send_key(k, False, upper)
            down_keys.discard(k)
            key_owner.pop(k, None)

    t0 = time.perf_counter()
    try:
        with Progress() as progress:
            task = progress.add_task(
                f"[cyan]键盘弹奏中" + (" · 按住时值" if hold else ""),
                total=len(events_sorted),
            )
            for (t, kind, e, planned) in sched:
                if stop_check is not None and stop_check():
                    stopped = True
                    break

                # ---- 焦点守卫：焦点不在目标窗口，就别把按键丢给浏览器 ----
                if focus_check is not None and not dry_run:
                    try:
                        ok, fg_title = focus_check()
                    except Exception:  # noqa: BLE001  检查本身出错时按「正常」处理
                        ok, fg_title = True, ""
                    if not ok:
                        if lost_mode == "stop":
                            stopped = True
                            _say("焦点不在目标窗口，已停止弹奏")
                            break
                        if lost_mode == "skip":
                            continue
                        # 默认 wait：先松开所有键，再等焦点回来（时间轴整体顺延）
                        _release_all()
                        _say(f"焦点不在目标窗口（当前：{fg_title or '未知窗口'}），已暂停 · "
                             f"切回目标窗口自动继续")
                        paused_at = time.perf_counter()
                        while True:
                            if stop_check is not None and stop_check():
                                stopped = True
                                break
                            try:
                                back, _t = focus_check()
                            except Exception:  # noqa: BLE001
                                back = True
                            if back:
                                break
                            time.sleep(0.1)
                        if stopped:
                            break
                        t0 += time.perf_counter() - paused_at   # 暂停多久，整体顺延多久
                        _say("焦点已回到目标窗口，继续弹奏")
                        continue

                now = time.perf_counter() - t0
                wait = max(0.0, t - now)
                if wait > 0:
                    # 可中断的等待：长休止段里也能立刻响应「停止/暂停」
                    if sleep_until(time.perf_counter() + wait, stop_check):
                        stopped = True
                        break
                target = time.perf_counter() - t0

                if dry_run:
                    logger.info(
                        "[dry-run] %s key=%s note=%s t=%.3f dur=%s",
                        kind, e.key, e.note, e.time, (e.duration or 0.0),
                    )
                else:
                    if kind == "down":
                        if e.key in down_keys:
                            _send_key(e.key, False, upper)  # 同键重触发：先抬再按
                            down_keys.discard(e.key)
                            key_owner.pop(e.key, None)
                        _send_key(e.key, True, upper)
                        down_keys.add(e.key)
                        key_owner[e.key] = e
                    else:
                        # 只有「这个键当前按下的仍是这条音符」才允许抬起。
                        # 否则上一条音的 up 会在新长音按住后把它掐断。
                        if (e.key in down_keys
                                and key_owner.get(e.key) is e):
                            _send_key(e.key, False, upper)
                            down_keys.discard(e.key)
                            key_owner.pop(e.key, None)
                if kind == "down":
                    deviations.append(target - planned)
                    progress.advance(task)
    except KeyboardInterrupt:
        raise
    finally:
        # 收尾：抬起所有还按着的键，避免卡键（异常退出也走这里）
        _release_all()
    if stopped:
        logger.info("键盘弹奏：收到停止请求")
    return deviations


# ---------------------------------------------------------------------------
# 按弹奏方式取校准（只读，不写文件）
# ---------------------------------------------------------------------------
def resolve_calibration(mode, cal_path) -> Calibration:
    """返回本次播放真正要用的那份校准。

    「原神键位优化」是一个勾选框（默认不勾），不勾就是老方案，
    所以这个函数**只读不写** —— 屏幕校准文件从头到尾不会被改，
    也就不存在「切过去把老数据覆盖掉、再切回来只能靠备份还原」的问题。
    """
    if is_keyboard_mode(mode):
        return build_keyboard_calibration()
    from .player import load_calibration

    return load_calibration(cal_path)


# ---------------------------------------------------------------------------
# 模块导出
# ---------------------------------------------------------------------------
