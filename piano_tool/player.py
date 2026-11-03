"""⑤ 模拟点击播放器

读取 events.json 与 calibration.json，按时间轴点击每个键对应的屏幕坐标。
支持：--speed / --delay-ms / --dry-run / --loop / --start-time / --end-time。
鼠标点击优先用 piano_tool.inject 的 SendInput（绝对坐标），失败回退 pyautogui。
FAILSAFE：鼠标移到屏幕四角可紧急停止。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import List, Optional

from .config import get_config
from .models import INPUT_MODE_KEYBOARD, Calibration, NoteEvent
from .waits import sleep_until

logger = logging.getLogger("piano_tool.player")

DEFAULT_HOLD_DURATION = 0.4   # 与编辑器 NOTE_DUR_DEFAULT 对齐：时值为空时按 0.4s


def load_events(path) -> List[NoteEvent]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [NoteEvent(**n) for n in raw]


def load_calibration(path) -> Calibration:
    return Calibration(**json.loads(Path(path).read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# 鼠标点击：优先 SendInput（绝对坐标），失败回退 pyautogui
# pyautogui 走的是 SetCursorPos + mouse_event 的老接口，一部分走 DirectInput /
# RawInput 的游戏收不到点击（表现为「鼠标移到位置了但不点」）；SendInput 的
# 绝对坐标 + 虚拟桌面方式兼容性更好。仍然是 Windows 标准 API，不读、不改游戏。
# FAILSAFE（鼠标移到屏幕四角急停）由 inject 内部实现，行为与 pyautogui 一致。
# ---------------------------------------------------------------------------
def _mouse_down(x, y, failsafe: bool = True) -> None:
    from . import inject

    if not inject.mouse_down(x, y, failsafe):
        import pyautogui

        pyautogui.mouseDown(x, y, _pause=False)


def _mouse_up(x, y, failsafe: bool = True) -> None:
    from . import inject

    if not inject.mouse_up(x, y, failsafe):
        import pyautogui

        pyautogui.mouseUp(x, y, _pause=False)


def _click(x, y) -> None:
    from . import inject

    if not inject.click(x, y):
        import pyautogui

        pyautogui.click(x, y, duration=0, _pause=False)


def build_hold_schedule(
    events_sorted: List[NoteEvent],
    key_to_coord: dict,
    base_lead: float,
    latency_lead,
    speed: float,
    start_time: float,
    hold_min: float,
    delay_ms: int,
    hold: bool,
):
    """把音符拆成 (按下→抬起) 时间线，按播放时刻升序返回。

    每条返回 (play_time, kind, event, x, y, planned_time)：
    - kind: "down" / "up"
    - planned_time: 该动作的预定播放时刻（用于计算时间偏差）
    非 hold 模式或没有时值的音符退化为「瞬时点击」：down 与 up 紧挨着，
    间隔 = hold_min + delay_ms（保持旧版点击间隔的手感）。
    """
    sched = []
    for e in events_sorted:
        x, y = key_to_coord.get(e.key, (0, 0))
        # 逐键延迟补偿已停用：latency_lead 不再参与触发时刻。
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
        sched.append((press_t, "down", e, x, y, press_t))
        sched.append((release_t, "up", e, x, y, release_t))
    sched.sort(key=lambda a: a[0])
    return sched


def play(
    events: List[NoteEvent],
    calibration: Calibration,
    speed: float = 1.0,
    delay_ms: int = 20,
    dry_run: bool = False,
    loop: bool = False,
    start_time: float = 0.0,
    end_time: Optional[float] = None,
    stop_check: Optional[callable] = None,
    latency_comp_ms: float = 0.0,
    latency_lead: Optional[Dict[str, float]] = None,
    hold: bool = False,
    hold_min_ms: float = 30.0,
    input_mode: str = "screen",
    focus_check: Optional[callable] = None,
    on_focus_lost: str = "wait",
    status: Optional[callable] = None,
):
    """按时间轴点击琴键。返回时间偏差列表（实际 - 计划，秒）。

    input_mode="keyboard"：走③ 的「键盘映射对应」分支（Q→U / A→J / Z→M），
    发送键盘按键而不是鼠标点击；其余参数含义与鼠标版完全相同。
    该分支会直接委托给 keyboard_play.play_keyboard，不读 calibration 的坐标。

    stop_check：可选回调，每个音符前调用一次，返回 True 时停止播放
    （供 GUI 的「停止」按钮使用）。

    latency_comp_ms：全局提前量（毫秒），统一作用于所有键。
    latency_lead：**已停用**（保留参数兼容）。④ 的逐键采样实际用途是给编辑器
        采集每键音色；逐键提前量会改变音与音的相对时间，同一份谱子会错拍/
        不和谐。播放只使用 latency_comp_ms 的统一提前量。

    hold：为 True 时按音符时值「按住」琴键（mouseDown→等待时值→mouseUp），
    还原长短/节奏；为 False 时退化为瞬时点击（旧行为）。
    hold_min_ms：最小按住时长（毫秒），防止 down/up 间隔过短导致游戏没收到。

    focus_check / on_focus_lost / status：焦点守卫，只对 input_mode="keyboard"
    生效（注入按键只对前台窗口生效，焦点跑了不该继续往浏览器发按键）。
    详见 keyboard_play.play_keyboard 与 piano_tool.focus_guard。
    """
    import pyautogui
    from rich.progress import Progress

    if not dry_run:
        pyautogui.FAILSAFE = True
        # 关键：关掉 pyautogui 默认的每次操作 100ms 暂停与最小移动时长，
        # 否则快速连音会被拖慢、每个点击都明显滞后于节拍。
        pyautogui.PAUSE = 0
        pyautogui.MINIMUM_DURATION = 0
        pyautogui.MINIMUM_SLEEP = 0

    if input_mode == INPUT_MODE_KEYBOARD:
        # 键盘映射方式：直接委托键盘播放器（不依赖任何屏幕坐标）
        from .keyboard_play import play_keyboard

        logger.info("弹奏方式：键盘映射（Q→U / A→J / Z→M），发送键盘按键")
        return play_keyboard(
            events,
            speed=speed,
            delay_ms=delay_ms,
            dry_run=dry_run,
            stop_check=stop_check,
            latency_comp_ms=latency_comp_ms,
            latency_lead=latency_lead,
            hold=hold,
            hold_min_ms=hold_min_ms,
            focus_check=focus_check,
            on_focus_lost=on_focus_lost,
            status=status,
        )

    # 校准坐标是「相对截图那块屏」的，加上该屏原点才是屏幕物理坐标。
    # 双屏时副屏在主屏左边/上边，虚拟屏原点为负，少了这一步会整体偏移。
    key_to_coord = {k.key: calibration.screen_point(k) for k in calibration.keys}
    events_sorted = sorted(events, key=lambda e: e.time)
    if end_time is not None:
        events_sorted = [e for e in events_sorted if e.time <= end_time]
    events_sorted = [e for e in events_sorted if e.time >= start_time]

    base_lead = latency_comp_ms / 1000.0
    deviations: List[float] = []
    loops = 0
    stopped = False
    hold_min = max(hold_min_ms / 1000.0, 0.0)

    try:
        while True:
            loops += 1
            t0 = time.perf_counter()
            with Progress() as progress:
                task = progress.add_task(
                    f"[cyan]弹奏中 (第 {loops} 轮)" + (" · 按住时值" if hold else ""),
                    total=len(events_sorted),
                )
                if hold:
                    sched = build_hold_schedule(
                        events_sorted, key_to_coord, base_lead, latency_lead,
                        speed, start_time, hold_min, delay_ms, hold,
                    )
                    down_keys = set()
                    key_owner = {}   # 键 -> 当前按下的音符事件（防止旧 up 掐断新长音）
                    for (t, kind, e, x, y, planned) in sched:
                        if stop_check is not None and stop_check():
                            logger.info("收到停止请求，结束播放")
                            stopped = True
                            break
                        now = time.perf_counter() - t0
                        wait = max(0.0, t - now)
                        if wait > 0:
                            # 可中断的等待：长休止段里也能立刻响应「停止/暂停」
                            if sleep_until(time.perf_counter() + wait, stop_check):
                                logger.info("收到停止请求，结束播放")
                                stopped = True
                                break
                        target = time.perf_counter() - t0

                        if dry_run:
                            logger.info(
                                "[dry-run] %s key=%s note=%s -> (%.1f, %.1f) t=%.3f dur=%.3f",
                                kind, e.key, e.note, x, y, e.time, (e.duration or 0.0),
                            )
                        else:
                            if (x, y) != (0, 0):
                                if kind == "down":
                                    if e.key in down_keys:
                                        # 同键在按住状态下被再次触发：先抬再按（重触发）
                                        _mouse_up(x, y)
                                        down_keys.discard(e.key)
                                        key_owner.pop(e.key, None)
                                    else:
                                        # 单鼠标键同一时刻只能按一个键。原神琴本就是单指
                                        # 弹奏，音符不应真正重叠。若上一个键还没抬起
                                        #（密集短音 / 重叠），必须先抬起它再按新键，
                                        # 否则新键的 mouseDown 会被当成“按钮已按下”而无声，
                                        # 表现为短音漏音 / 被截断 → 听感卡顿。
                                        for pk in list(down_keys):
                                            px, py = key_to_coord.get(pk, (0, 0))
                                            if (px, py) != (0, 0):
                                                _mouse_up(px, py)
                                            down_keys.discard(pk)
                                            key_owner.pop(pk, None)
                                    _mouse_down(x, y)
                                    down_keys.add(e.key)
                                    key_owner[e.key] = e
                                else:  # up
                                    # 只有这条音符仍是该键的主人时才抬起；旧 up 不能
                                    # 把之后重新按住的新长音掐断。
                                    if (e.key in down_keys
                                            and key_owner.get(e.key) is e):
                                        _mouse_up(x, y)
                                        down_keys.discard(e.key)
                                        key_owner.pop(e.key, None)
                            else:
                                logger.warning("未找到键 %s 的坐标，跳过", e.key)

                        if kind == "down":
                            deviations.append(target - planned)
                            progress.advance(task)
                    # 收尾：抬起所有仍按住的键，避免卡键
                    for k in list(down_keys):
                        x, y = key_to_coord.get(k, (0, 0))
                        if (x, y) != (0, 0) and not dry_run:
                            _mouse_up(x, y)
                        down_keys.discard(k)
                        key_owner.pop(k, None)
                else:
                    for e in events_sorted:
                        if stop_check is not None and stop_check():
                            logger.info("收到停止请求，结束播放")
                            stopped = True
                            break
                        sched = (e.time - start_time) / speed
                        now = time.perf_counter() - t0
                        # 统一提前量；逐键延迟补偿已停用（会打乱相对节奏）
                        lead = base_lead
                        wait = max(0.0, sched - now - lead)
                        if wait > 0:
                            time.sleep(wait)
                        target = time.perf_counter() - t0

                        x, y = key_to_coord.get(e.key, (0, 0))
                        if dry_run:
                            logger.info(
                                "[dry-run] key=%s note=%s -> (%.1f, %.1f) t=%.3f",
                                e.key,
                                e.note,
                                x,
                                y,
                                e.time,
                            )
                        else:
                            if (x, y) != (0, 0):
                                # duration=0 瞬时移动
                                _click(x, y)
                            else:
                                logger.warning("未找到键 %s 的坐标，跳过", e.key)

                        deviations.append(target - sched)
                        progress.advance(task)
                        if delay_ms:
                            time.sleep(delay_ms / 1000.0)
            if stopped or not loop:
                break
    except pyautogui.FailSafeException:
        logger.warning("触发 FAILSAFE（鼠标移到屏幕四角），紧急停止")
        # 抬起所有可能仍按住的键，避免卡键。
        # 此刻指针正停在屏幕角落，必须跳过四角检查（failsafe=False），
        # 否则收尾本身会再次触发急停、把抬键动作打断。
        if not dry_run:
            for (kx, ky) in key_to_coord.values():
                if (kx, ky) != (0, 0):
                    try:
                        _mouse_up(kx, ky, failsafe=False)
                    except Exception:  # noqa: BLE001
                        pass
    return deviations


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m piano_tool.player", description="模拟点击播放"
    )
    parser.add_argument("events", help="events.json 路径")
    parser.add_argument("--calibration", default=None, help="calibration.json 路径")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--delay-ms", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--start-time", type=float, default=0.0)
    parser.add_argument("--end-time", type=float, default=None)
    parser.add_argument("--latency-comp-ms", type=float, default=0.0,
                        help="全局提前量(毫秒)：让点击提前触发以抵消点击→发声的延迟（兜底用）")
    parser.add_argument("--latency-profile", default=None,
                        help="（已停用，仅保留兼容）逐键样本库；④ 采样实际用于采集编辑器音色")
    parser.add_argument("--hold", action="store_true",
                        help="按音符时值按住琴键（还原长短/节奏），否则瞬时点击")
    parser.add_argument("--hold-min-ms", type=float, default=30.0,
                        help="最小按住时长(毫秒)，防止 down/up 过快游戏没收到")
    parser.add_argument("--input-mode", default="screen", choices=["screen", "keyboard"],
                        help="弹奏方式：screen（屏幕校准+鼠标点击，默认）/ keyboard（键盘映射 Q-U/A-J/Z-M）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = get_config()
    cal_path = args.calibration or cfg.calibration_path
    events = load_events(args.events)
    calibration = None
    if args.input_mode != "keyboard":
        # 键盘映射方式不需要坐标，不必强制读校准文件
        calibration = load_calibration(cal_path)
    lead = None
    if args.latency_profile:
        # 逐键延迟补偿已停用：④ 的采样实际用途是采集每键音色给编辑器模拟音用。
        # 逐键提前量会改变音与音的相对时间，同一份谱子会错拍/不和谐。
        logger.warning("逐键延迟补偿已停用（④ 采样用于采集编辑器音色）；"
                       "--latency-profile 不再影响播放，只使用统一提前量")
    devs = play(
        events,
        calibration,
        speed=args.speed,
        delay_ms=args.delay_ms,
        dry_run=args.dry_run,
        loop=args.loop,
        start_time=args.start_time,
        end_time=args.end_time,
        latency_comp_ms=args.latency_comp_ms,
        latency_lead=lead,
        hold=args.hold,
        hold_min_ms=args.hold_min_ms,
        input_mode=args.input_mode,
    )
    if devs:
        import statistics

        print(
            f"✓ 播放完成：{len(devs)} 次点击，"
            f"平均偏差 {statistics.mean(devs)*1000:.1f} ms，"
            f"最大 {max(devs)*1000:.1f} ms，"
            f"抖动(标准差) {statistics.pstdev(devs)*1000:.1f} ms"
        )


if __name__ == "__main__":
    main()
