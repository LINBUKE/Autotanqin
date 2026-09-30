"""⑥ 端到端测试（浏览器模拟器）

打开风物之诗琴网页模拟器，提示用户完成四点校准，先播放 C 大调音阶
验证坐标，再播放一段简单 MIDI，输出 data/simulator_test_report.json。

仅针对浏览器模拟器，不连接游戏本体。
"""
from __future__ import annotations

import json
import logging
import webbrowser
from pathlib import Path

from .config import get_config
from .models import NoteEvent
from .player import load_calibration, play

logger = logging.getLogger("piano_tool.simulator_test")

# 风物之诗琴社区模拟器（浏览器版）。如地址变动请更新。
SIMULATOR_URL = "https://seliforg.github.io/WindsongLyre-Sim/"


def c_major_scale_events() -> list:
    """C 大调音阶 C4..B4：Z X C V B N M。"""
    from .config import WHITE_KEYS

    scale = [60, 62, 64, 65, 67, 69, 71]
    by_midi = {int(k["midi"]): k for k in WHITE_KEYS}
    events = []
    for i, m in enumerate(scale):
        k = by_midi[m]
        events.append(
            NoteEvent(time=i * 0.5, note=m, key=k["key"], action="press", duration=0.4)
        )
    return events


def run(midi_path=None, simulator_url: str = SIMULATOR_URL, report_path=None):
    cfg = get_config()
    report_path = Path(report_path or cfg.report_path)

    print(f"将在浏览器打开模拟器：{simulator_url}")
    webbrowser.open(simulator_url)

    input("请在模拟器中把琴键区域完整显示，完成后按回车继续…")

    # 第一步：校准（若已存在则复用）
    cal_path = cfg.calibration_path
    if not Path(cal_path).exists():
        from .locator import calibrate

        calibrate(calibration_path=cal_path)
    calibration = load_calibration(cal_path)

    report = {"steps": [], "ok": True}

    # 第二步：C 大调音阶坐标验证
    print("▶ 播放 C 大调音阶验证坐标…")
    play(c_major_scale_events(), calibration, dry_run=False, speed=1.0)
    ok_scale = input("音阶 Q W E R T Y U / Z X C V B N M 是否全部正确发声？(y/n) ").strip().lower() == "y"
    report["steps"].append(
        {"name": "c_major_scale", "ok": ok_scale, "note": "验证 21 键坐标映射"}
    )
    report["ok"] = report["ok"] and ok_scale

    # 第三步：简单 MIDI 播放
    if midi_path:
        from .midi_mapper import map_midi_to_events, write_events

        events, _ = map_midi_to_events(midi_path)
        events_path = cfg.output_dir / "sim_events.json"
        write_events(events, events_path)
        print(f"▶ 播放 MIDI：{midi_path}")
        play(events, calibration, dry_run=False, speed=1.0)
        ok_midi = input("MIDI 播放是否与预期一致？(y/n) ").strip().lower() == "y"
        report["steps"].append(
            {"name": "simple_midi", "ok": ok_midi, "events": str(events_path)}
        )
        report["ok"] = report["ok"] and ok_midi

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info("测试报告：%s", report_path)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m piano_tool.simulator_test", description="端到端测试"
    )
    parser.add_argument("--midi", default=None, help="可选：用于测试的 MIDI 路径")
    parser.add_argument("--url", default=SIMULATOR_URL)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    report = run(midi_path=args.midi, simulator_url=args.url)
    print("✓ 端到端测试完成，结果：", "通过" if report["ok"] else "未通过")


if __name__ == "__main__":
    main()
