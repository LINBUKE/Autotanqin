"""风物之诗琴工具 · 一键启动"""
import subprocess
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).parent
EDITOR = ROOT / "editor" / "editor.html"


def main():
    print("=" * 60)
    print("  风物之诗琴自动化工具 · 启动菜单")
    print("=" * 60)
    print("  1. 打开桌面版 GUI（推荐）")
    print("  2. 打开网页控制台（本地 Flask GUI）")
    print("  3. 打开可视化编辑器（浏览器）")
    print("  4. 音频转 MIDI")
    print("  5. MIDI 映射为琴谱")
    print("  6. 校准琴键坐标")
    print("  7. 模拟点击播放")
    print("  8. 端到端测试（模拟器）")
    print("  0. 退出")
    print("=" * 60)

    choice = input("请选择: ").strip()

    if choice == "1":
        subprocess.run([sys.executable, "desktop_app.py"])
    elif choice == "2":
        # 网页控制台：启动 Flask 并打开浏览器
        import webbrowser as _wb
        import threading as _th

        url = "http://127.0.0.1:8000"
        _th.Timer(1.0, lambda: _wb.open(url)).start()
        subprocess.run([sys.executable, "app.py"])
    elif choice == "3":
        webbrowser.open(EDITOR.as_uri())
    elif choice == "4":
        inp = input("输入音频路径: ").strip()
        subprocess.run([sys.executable, "-m", "piano_tool.audio_to_midi", inp])
    elif choice == "5":
        inp = input("输入 MIDI 路径: ").strip()
        out = input("输出 events.json 路径（回车用默认）: ").strip() or "data/output/events.json"
        subprocess.run([sys.executable, "-m", "piano_tool.midi_mapper", inp, "-o", out])
    elif choice == "6":
        subprocess.run([sys.executable, "-m", "piano_tool.locator", "calibrate"])
    elif choice == "7":
        inp = input("events.json 路径: ").strip()
        subprocess.run([sys.executable, "-m", "piano_tool.player", inp])
    elif choice == "8":
        subprocess.run([sys.executable, "-m", "piano_tool.simulator_test"])
    else:
        print("退出")


if __name__ == "__main__":
    main()
