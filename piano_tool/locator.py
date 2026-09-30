"""④ 四点定位校准

对模拟器琴键区域截图，让用户点击四个角，用 OpenCV 透视变换把
3 行 × 7 列的均匀网格映射到 21 个键中心坐标，保存 calibration.json
并生成可视化调试图 data/debug_calibration.png。

纯几何函数 compute_key_coordinates 不依赖显示，可单测。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Tuple

import numpy as np

from .config import WHITE_KEYS, NAME_BY_MIDI
from .models import Calibration, KeyCoordinate

logger = logging.getLogger("piano_tool.locator")

ROWS = 3
COLS = 7


def compute_key_coordinates(
    corners: List[List[float]], rows: int = ROWS, cols: int = COLS
) -> List[Tuple[float, float]]:
    """给定四角（TL, TR, BR, BL），返回 21 个键中心坐标（原始图像坐标）。

    corners 顺序必须是：左上、右上、右下、左下。
    """
    import cv2

    corners = np.array(corners, dtype=np.float32)
    # 目标规则矩形（列方向为 x，行方向为 y）
    dst = np.array(
        [[0, 0], [cols, 0], [cols, rows], [0, rows]], dtype=np.float32
    )
    M = cv2.getPerspectiveTransform(corners, dst)
    Minv = np.linalg.inv(M)

    coords: List[Tuple[float, float]] = []
    for r in range(rows):
        for c in range(cols):
            # 单元格 (r, c) 的中心在目标空间为 (c+0.5, r+0.5)
            p = np.array([c + 0.5, r + 0.5, 1.0], dtype=np.float32)
            orig = Minv @ p
            orig = orig / orig[2]
            coords.append((float(orig[0]), float(orig[1])))
    return coords


def assign_keys_to_coords(
    coords: List[Tuple[float, float]]
) -> List[KeyCoordinate]:
    """把 21 个坐标按音高升序分配到键（逐行对应）。"""
    keys_sorted = sorted(WHITE_KEYS, key=lambda k: int(k["midi"]))
    result: List[KeyCoordinate] = []
    for (x, y), k in zip(coords, keys_sorted):
        result.append(
            KeyCoordinate(
                key=str(k["key"]),
                midi=int(k["midi"]),
                name=str(k["name"]),
                x=x,
                y=y,
            )
        )
    return result


def build_calibration(
    corners: List[List[float]], image_size=None, rows: int = ROWS, cols: int = COLS
) -> Calibration:
    coords = compute_key_coordinates(corners, rows, cols)
    keys = assign_keys_to_coords(coords)
    return Calibration(
        corners=[[float(v) for v in c] for c in corners],
        keys=keys,
        image_size=image_size,
    )


def save_calibration(cal: Calibration, path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(cal.model_dump_json(indent=2), encoding="utf-8")
    return out


def draw_debug_image(image, cal: Calibration, path) -> Path:
    """在截图上画出四角与 21 个键中心，便于人工检查。

    使用 imio.imwrite_unicode：Windows 上 cv2.imwrite 不支持中文路径。
    """
    import cv2

    from .imio import imwrite_unicode

    out = Path(path)
    vis = image.copy()
    for (x, y) in cal.corners:
        cv2.circle(vis, (int(x), int(y)), 8, (0, 0, 255), -1)
    for k in cal.keys:
        cv2.circle(vis, (int(k.x), int(k.y)), 5, (0, 255, 0), -1)
        cv2.putText(
            vis,
            k.key,
            (int(k.x) + 6, int(k.y) + 4),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.4,
            (255, 255, 0),
            1,
        )
    if not imwrite_unicode(out, vis):
        logger.warning("调试图写入失败：%s", out)
    return out


def calibrate(calibration_path=None, debug_path=None, screenshot_path=None):
    """交互式四点校准。

    - 若提供 screenshot_path，则直接读图；否则用 mss 截屏。
    - 弹出 OpenCV 窗口，按 左上为起点 依次点击 四角（TL, TR, BR, BL），
      按任意键/ESC 结束后计算并保存。
    """
    import cv2

    from .config import get_config

    cfg = get_config()
    calibration_path = Path(calibration_path or cfg.calibration_path)
    debug_path = Path(debug_path or cfg.debug_image)

    if screenshot_path:
        from .imio import imread_unicode

        image = imread_unicode(screenshot_path)
        if image is None:
            raise FileNotFoundError(f"无法读取截图：{screenshot_path}")
    else:
        import mss

        from .imio import imread_unicode

        with mss.mss() as sct:
            shot = sct.shot(output=str(cfg.data_dir / "screenshot.png"))
        image = imread_unicode(shot)
        if image is None:
            raise FileNotFoundError(f"无法读取截屏文件：{shot}")

    h, w = image.shape[:2]
    clicked = []

    def on_click(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and len(clicked) < 4:
            clicked.append([x, y])
            cv2.circle(image, (x, y), 8, (0, 0, 255), -1)
            cv2.imshow("calibrate", image)
            print(f"已记录第 {len(clicked)} 个点：({x}, {y})")

    cv2.imshow("calibrate", image)
    cv2.setMouseCallback("calibrate", on_click)
    print("请依次点击四个角：左上 → 右上 → 右下 → 左下，然后按 ESC 结束")
    while True:
        if cv2.waitKey(0) == 27 or len(clicked) >= 4:
            break
    cv2.destroyAllWindows()

    if len(clicked) != 4:
        raise RuntimeError(f"需要点击 4 个点，实际点击了 {len(clicked)} 个")

    cal = build_calibration(clicked, image_size=[w, h])
    save_calibration(cal, calibration_path)
    draw_debug_image(image, cal, debug_path)
    logger.info("校准完成：%s", calibration_path)
    return cal


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m piano_tool.locator",
        description="四点定位校准琴键坐标",
    )
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("calibrate", help="交互式校准")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.cmd == "calibrate":
        cal = calibrate()
        print(f"✓ 校准完成，21 个键坐标已保存")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
