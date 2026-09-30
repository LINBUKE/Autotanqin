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
):
    """② MIDI → events.json。"""
    _setup_log()
    from .midi_mapper import map_midi_to_events, write_events

    events, stats = map_midi_to_events(input, bpm=bpm)
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
