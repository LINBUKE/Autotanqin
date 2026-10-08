"""⑤ 模拟点击播放器

读取 events.json 与 calibration.json，用 pyautogui 按时间轴点击每个键对应的屏幕坐标。
支持：--speed / --delay-ms / --dry-run / --loop / --start-time / --end-time。
FAILSAFE：鼠标移到屏幕左上角可紧急停止。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import List, Optional

from .config import get_config
from .models import INPUT_MODE_KEYBOARD, Calibration, NoteEvent

logger = logging.getLogger("piano_tool.player")


def load_events(path) -> List[NoteEvent]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [NoteEvent(**n) for n in raw]


def load_calibration(path) -> Calibration:
    return Calibration(**json.loads(Path(path).read_text(encoding="utf-8")))


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
        lead = base_lead + (latency_lead.get(e.key, 0.0) if latency_lead else 0.0)
        press_t = (e.time - start_time) / speed - lead
        dur = e.duration or 0.0
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

    latency_comp_ms：全局提前量（毫秒），手动兜底用。
    latency_lead：逐键提前量（秒）{键: 秒}，来自「逐键延迟采样」样本库，
    优先于 latency_comp_ms（两者叠加）。让每个键的点击按其实测延迟提前触发，
    声音更准地落在节拍上。

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

    key_to_coord = {k.key: (k.x, k.y) for k in calibration.keys}
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
                    for (t, kind, e, x, y, planned) in sched:
                        if stop_check is not None and stop_check():
                            logger.info("收到停止请求，结束播放")
                            stopped = True
                            break
                        now = time.perf_counter() - t0
                        wait = max(0.0, t - now)
                        if wait > 0:
                            time.sleep(wait)
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
                                        pyautogui.mouseUp(x, y, _pause=False)
                                        down_keys.discard(e.key)
                                    else:
                                        # 单鼠标键同一时刻只能按一个键。原神琴本就是单指
                                        # 弹奏，音符不应真正重叠。若上一个键还没抬起
                                        #（密集短音 / 重叠），必须先抬起它再按新键，
                                        # 否则新键的 mouseDown 会被当成“按钮已按下”而无声，
                                        # 表现为短音漏音 / 被截断 → 听感卡顿。
                                        for pk in list(down_keys):
                                            px, py = key_to_coord.get(pk, (0, 0))
                                            if (px, py) != (0, 0):
                                                pyautogui.mouseUp(px, py, _pause=False)
                                            down_keys.discard(pk)
                                    pyautogui.mouseDown(x, y, _pause=False)
                                    down_keys.add(e.key)
                                else:  # up
                                    if e.key in down_keys:
                                        pyautogui.mouseUp(x, y, _pause=False)
                                        down_keys.discard(e.key)
                            else:
                                logger.warning("未找到键 %s 的坐标，跳过", e.key)

                        if kind == "down":
                            deviations.append(target - planned)
                            progress.advance(task)
                    # 收尾：抬起所有仍按住的键，避免卡键
                    for k in list(down_keys):
                        x, y = key_to_coord.get(k, (0, 0))
                        if (x, y) != (0, 0) and not dry_run:
                            pyautogui.mouseUp(x, y, _pause=False)
                        down_keys.discard(k)
                else:
                    for e in events_sorted:
                        if stop_check is not None and stop_check():
                            logger.info("收到停止请求，结束播放")
                            stopped = True
                            break
                        sched = (e.time - start_time) / speed
                        now = time.perf_counter() - t0
                        # 提前量：逐键样本 + 全局兜底，在节拍点之前触发点击，
                        # 抵消「点击→发声」的固有延迟
                        lead = base_lead + (latency_lead.get(e.key, 0.0) if latency_lead else 0.0)
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
                                # duration=0 瞬时移动；_pause=False 绕过全局 PAUSE
                                pyautogui.click(x, y, duration=0, _pause=False)
                            else:
                                logger.warning("未找到键 %s 的坐标，跳过", e.key)

                        deviations.append(target - sched)
                        progress.advance(task)
                        if delay_ms:
                            time.sleep(delay_ms / 1000.0)
            if stopped or not loop:
                break
    except pyautogui.FailSafeException:
        logger.warning("触发 FAILSAFE（鼠标移到左上角），紧急停止")
        # 抬起所有可能仍按住的键，避免卡键
        if not dry_run:
            for (kx, ky) in key_to_coord.values():
                if (kx, ky) != (0, 0):
                    try:
                        pyautogui.mouseUp(kx, ky, _pause=False)
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
                        help="逐键延迟样本库路径（key_latency.json），优先于全局提前量")
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
        from .calibrate_latency import load_latency_profile, profile_to_lead_seconds

        prof = load_latency_profile(args.latency_profile)
        if prof:
            lead = profile_to_lead_seconds(prof)
            logger.info("已加载逐键延迟样本库：%d 个键，均值 %.1f ms",
                        len(lead), prof.get("mean_ms") or 0)
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
            f"平均偏差 {statistics.mean(devs)*1000:.1f} ms"
        )


if __name__ == "__main__":
    main()
