"""④ 音频自检：把「系统声音(回环)」逐块听一遍，看谁此刻真的有声音。

双击发布包里的 `音频自检.bat` 就会跑。用途是在**调参数之前**先排除环境问题 ——
④ 采样「一个声音都没有」时，先跑这个，一眼能看出是哪种情况：

- **没有任何回环设备** → pyaudiowpatch 没装上，或系统没有输出设备；
- **设备都在，但每块峰值都是 0.00000** → 系统此刻没有任何音频在播放
  （游戏没出声 / 被静音 / 声音走了另一块没列出来的设备）；
- **只有某一块有峰值** → 那一块就是该选的（④ 页下拉里选它，或点「🎯 找正在出声的」）。

为什么录屏软件能录、本程序不一定能录：录屏软件绑的是「系统默认输出的混音总线」，
而且会自动跟着系统默认设备走；本程序为了「只测模拟器那块」必须**指定设备**，
一旦指定的那块和游戏实际出声的那块不是同一块（蓝牙 / HDMI / 多显示器场景很常见），
就会完整地录下 1 秒钟的静音 —— 开流成功、没有报错，这才是最难查的。
"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from piano_tool.latency_probe import (
            list_capture_devices,
            probe_loopback_levels,
            pyaudio_loopback_devices,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 加载音频模块失败：{exc}")
        return 1

    print("=" * 66)
    print("风物之诗琴 · 音频自检")
    print("=" * 66)
    print("请先让模拟器/游戏**持续出声**（一直弹几个音），再回车开始检查。")
    try:
        input("按回车开始…")
    except EOFError:
        pass

    loops = pyaudio_loopback_devices()
    if not loops:
        print("\n✗ 没有找到任何「系统声音(回环)」设备。")
        print("  多半是没装 pyaudiowpatch：双击「启动.bat」会自动装；")
        print("  手动装：.venv\\Scripts\\python.exe -m pip install pyaudiowpatch")
        return 1

    print(f"\n找到 {len(loops)} 块可以回环采集的输出设备，逐块听 0.6 秒…\n")
    results = probe_loopback_levels(loops, seconds=0.6)
    best = max(results, key=lambda r: float(r.get("peak") or 0.0), default=None)
    peak_best = float((best or {}).get("peak") or 0.0)

    for r in results:
        idx = r.get("index")
        name = r.get("name") or "?"
        peak = float(r.get("peak") or 0.0)
        err = r.get("error")
        mark = ""
        if best is not None and r is best and peak_best > 1e-5:
            mark = "   ← 这块正在出声（④ 页选它）"
        if err:
            print(f"  [{idx:>2}] 打不开：{err}")
        else:
            print(f"  [{idx:>2}] 峰值 {peak:.5f}  {name}{mark}")

    print()
    if peak_best <= 1e-5:
        print("✗ 所有设备都没有声音。这不是程序的问题，是**此刻系统里没有音频在播放**：")
        print("  ① 游戏/模拟器真的在出声吗？（不是只开了窗口）")
        print("  ② 任务栏右下角音量图标，点开看**当前输出设备**是哪一块；")
        print("  ③ 那块设备有没有被静音、音量是不是 0；")
        print("  ④ 蓝牙音箱要先连上并真的在放声音（有些设备待机时静音）。")
        print("\n  确认有声音后，再跑一次本自检 —— 应该能看到某一块峰值明显大于 0。")
        return 2

    print(f"✓ 有设备在出声：{best.get('name')}")
    print("  ④ 页「录制设备」下拉里选这一块，再点「开始逐键采样」即可。")
    print("  （程序现在也会自动做这件事：采样前先听 1 秒，听不到就自己换一块。）")

    # 顺便报告输入设备，便于判断有没有误选麦克风
    try:
        caps = list_capture_devices()
        inputs = [d for d in caps if not d.get("loopback")]
        if inputs:
            print(f"\n另外还有 {len(inputs)} 个『麦克风/立体声混音』输入设备——")
            print("  它们录的是环境声，不是游戏声音。采样**不要**选这些。")
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
