"""统一命令行入口（typer）

子命令：
    piano-tool convert  input.mp3 -o output.mid
    piano-tool map      output.mid -o events.json
    piano-tool calibrate
    piano-tool play     events.json --speed 1.0
    piano-tool test-simulator --midi output.mid
"""
from __future__ import annotations

import logging

import typer
from rich import print as rprint
from rich.console import Console
from rich.logging import RichHandler

from . import __version__
from .config import get_config

app = typer.Typer(help="风物之诗琴自动化工具", add_completion=False)
console = Console()


def _setup_log():
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


@app.command()
def convert(
    input: str = typer.Argument(..., help="输入音频 MP3/WAV"),
    output: str = typer.Option("data/output/song.mid", "-o", "--output"),
):
    """① 音频 → MIDI。"""
    _setup_log()
    from .audio_to_midi import audio_to_midi

    count, out = audio_to_midi(input, output)
    rprint(f"[green]✓[/green] 转写完成：{count} 音符 -> {out}")


@app.command()
def map(
    input: str = typer.Argument(..., help="输入 MIDI"),
    output: str = typer.Option("data/output/events.json", "-o", "--output"),
    bpm: int = typer.Option(120, "--bpm"),
    arrange: str = typer.Option(
        "none", "--arrange",
        help="声部整理方式：none=老行为(默认) / track=独立分轨(推荐) / "
             "pitch=按音高分三段 / raw=原样多声部"),
    polyphony: int = typer.Option(
        0, "--polyphony",
        help="同时最多几个音（0=不限制；仅 --arrange track/pitch 生效）"),
):
    """② MIDI → events.json。"""
    _setup_log()
    from .midi_mapper import map_midi_to_events, write_events

    arrange = str(arrange or "none").strip().lower()
    if arrange not in ("none", "track", "pitch", "raw"):
        rprint(f"[yellow]⚠ 未知 --arrange {arrange!r}，按 none（老行为）处理[/yellow]")
        arrange = "none"
    events, stats = map_midi_to_events(
        input, bpm=bpm, arrange=arrange, polyphony=polyphony)
    out = write_events(events, output)
    rprint(f"[green]✓[/green] 映射完成：{stats['total']} 音符 -> {out}")
    rprint(
        f"  黑键替换 {stats['black_replaced']} · "
        f"八度折叠 {stats['octave_folded']} · "
        f"白键保留 {stats['white_kept']}"
    )


@app.command()
def calibrate():
    """④ 四点定位校准。"""
    _setup_log()
    from .locator import calibrate as do_calibrate

    do_calibrate()
    rprint("[green]✓[/green] 校准完成")


@app.command()
def play(
    events: str = typer.Argument(..., help="events.json"),
    calibration: str = typer.Option(None, "-c", "--calibration"),
    speed: float = typer.Option(1.0, "-s", "--speed"),
    delay_ms: int = typer.Option(20, "-d", "--delay-ms"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    loop: bool = typer.Option(False, "--loop"),
):
    """⑤ 模拟点击播放。"""
    _setup_log()
    from .player import load_events, load_calibration, play as do_play

    # 谱子自带的乐器（编辑器保存时写的 events_instrument.json）：
    # 不同乐器的键位字母不同，不一致时提醒（不静默改当前乐器）
    try:
        from pathlib import Path as _Path
        from . import instruments as _inst
        _meta = _Path(events).parent / "events_instrument.json"
        if _meta.exists():
            import json as _json
            _spec = _json.loads(_meta.read_text(encoding="utf-8")).get("instrument")
            if _spec:
                _cur = _inst.active()
                if str(getattr(_cur, "id", "")) != str(_spec):
                    _i = _inst.get_instrument(_spec)
                    rprint(f"[yellow]⚠[/yellow] 这份谱子的键位是按「{_i.name}」生成的，"
                           f"当前乐器是「{_cur.name}」—— 字母会对不上，"
                           f"请先切换到「{_i.name}」再弹")
    except Exception as exc:  # noqa: BLE001
        rprint(f"[yellow]⚠[/yellow] 读 events_instrument.json 失败：{exc}")

    cfg = get_config()
    cal = load_calibration(calibration or cfg.calibration_path)
    evs = load_events(events)
    do_play(evs, cal, speed=speed, delay_ms=delay_ms, dry_run=dry_run, loop=loop)
    rprint("[green]✓[/green] 播放完成")


@app.command("test-simulator")
def test_simulator(
    midi: str = typer.Option(None, "-m", "--midi"),
    url: str = typer.Option("https://seliforg.github.io/WindsongLyre-Sim/"),
):
    """⑥ 端到端测试。"""
    _setup_log()
    from .simulator_test import run as do_run

    report = do_run(midi_path=midi, simulator_url=url)
    rprint(
        f"[green]✓[/green] 端到端测试："
        f"{'通过' if report['ok'] else '[red]未通过[/red]'}"
    )


@app.command()
def version():
    """显示版本。"""
    rprint(f"piano-tool {__version__}")


if __name__ == "__main__":
    app()
