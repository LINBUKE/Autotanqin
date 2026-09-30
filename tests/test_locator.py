"""④ locator 测试：固定四点矩形，验证 21 键坐标误差 < 2 像素。"""
import numpy as np
import pytest

from piano_tool.locator import compute_key_coordinates, assign_keys_to_coords, build_calibration, ROWS, COLS

# 矩形：左上(100,100) 右上(800,100) 右下(800,400) 左下(100,400)
# 7 列 → 每列 100px；3 行 → 每行 100px
CORNERS = [[100, 100], [800, 100], [800, 400], [100, 400]]


def _expected(r, c):
    return (100 + (c + 0.5) * 100, 100 + (r + 0.5) * 100)


def test_compute_21_keys_within_2px():
    coords = compute_key_coordinates(CORNERS, rows=ROWS, cols=COLS)
    assert len(coords) == ROWS * COLS == 21
    for r in range(ROWS):
        for c in range(COLS):
            (x, y) = coords[r * COLS + c]
            ex, ey = _expected(r, c)
            assert abs(x - ex) < 2.0, f"key({r},{c}) x 误差过大: {x} vs {ex}"
            assert abs(y - ey) < 2.0, f"key({r},{c}) y 误差过大: {y} vs {ey}"


def test_calibration_has_21_key_coords():
    cal = build_calibration(CORNERS, image_size=[900, 500])
    assert len(cal.keys) == 21
    # 键坐标应与计算一致
    coords = compute_key_coordinates(CORNERS)
    for k, (x, y) in zip(cal.keys, coords):
        assert abs(k.x - x) < 1e-6
        assert abs(k.y - y) < 1e-6
    # 俯仰顺序：最低音 C4 应出现在某格
    midis = [k.midi for k in cal.keys]
    assert 60 in midis and 95 in midis
