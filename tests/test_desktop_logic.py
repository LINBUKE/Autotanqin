"""桌面版 GUI 纯逻辑函数测试（不创建 Tk 实例，不依赖显示）。"""
import json

import desktop_app as mod


def test_place_default_corners_shape_and_order():
    corners = mod.place_default_corners(1920, 1080)
    assert len(corners) == 4
    tl, tr, br, bl = corners
    # 顺序：左上、右上、右下、左下
    assert tl[0] < tr[0] and tl[1] < bl[1]
    assert br[0] > bl[0] and br[1] > tr[1]
    assert all(0 <= x <= 1920 and 0 <= y <= 1080 for x, y in corners)


def test_find_nearest_corner_hits_and_misses():
    corners = [[0, 0], [100, 0], [100, 100], [0, 100]]
    assert mod.find_nearest_corner(corners, 3, 4, radius=10) == 0
    assert mod.find_nearest_corner(corners, 97, 2, radius=10) == 1
    assert mod.find_nearest_corner(corners, 98, 98, radius=10) == 2
    # 太远 → None
    assert mod.find_nearest_corner(corners, 50, 50, radius=10) is None


def test_inject_events_into_editor_before_body_close():
    html = "<html><body><div id=app></div></body></html>"
    events = [{"time": 0.5, "note": 60, "key": "Z", "action": "press"}]
    out = mod.inject_events_into_editor(html, json.dumps(events))
    assert "loadNotesFromArray" in out
    # 注入的内容必须在 </body> 之前，确保 DOM ready 前脚本已存在
    assert out.index("loadNotesFromArray") < out.index("</body>")
    # 原有内容保持完整
    assert "<div id=app></div>" in out


def test_inject_events_into_editor_without_body_tag():
    out = mod.inject_events_into_editor("<p>x</p>", "[]")
    assert out.endswith("[])}catch(e){console.error('inject events failed',e);}});</script>")


def test_basic_pitch_available_returns_tuple():
    ok, msg = mod.basic_pitch_available()
    assert isinstance(ok, bool)
    if not ok:
        assert msg  # 失败时要有原因


def test_check_coords_on_screen_inside():
    """坐标都在屏幕内 → 通过。显式传 screen_size，避免 patch sys.modules。"""
    from piano_tool.locator import build_calibration

    cal = build_calibration([[100, 100], [800, 100], [800, 400], [100, 400]],
                            image_size=[900, 500])
    ok, msg = mod.check_coords_on_screen(cal, screen_size=(2560, 1600))
    assert ok is True
    assert "21 键坐标均在屏幕内" in msg


def test_check_coords_on_screen_outside():
    """屏幕很小 → 坐标越界，应报 False 并指出越界键数量。"""
    from piano_tool.locator import build_calibration

    cal = build_calibration([[100, 100], [800, 100], [800, 400], [100, 400]],
                            image_size=[900, 500])
    ok, msg = mod.check_coords_on_screen(cal, screen_size=(200, 200))
    assert ok is False
    assert "超出屏幕" in msg


def test_build_events_json_from_preserves_duration():
    """浏览器编辑器「保存」回传的数据要保留时值，含 null（单点）。"""
    data = [
        {"time": 1.23456, "note": 60, "key": "Z", "action": "press", "duration": 0.195},
        {"time": 2.0, "note": 62, "key": "X", "action": "press", "duration": None},
    ]
    out = mod._build_events_json_from(data)
    assert out[0]["duration"] == 0.195
    assert out[0]["time"] == 1.235  # round(1.23456, 3) 进位
    assert out[1]["duration"] is None
    assert out[1]["action"] == "press"


def test_editor_server_serves_and_saves(tmp_path, monkeypatch):
    """真实起一个 127.0.0.1 本地服务，验证 ?events 加载与保存回写。"""
    import urllib.request

    from http.server import ThreadingHTTPServer

    out_dir = tmp_path / "output"
    out_dir.mkdir()
    events_src = [
        {"time": 0.1, "note": 60, "key": "Z", "action": "press", "duration": 0.2},
    ]
    (out_dir / "events.json").write_text(
        json.dumps(events_src, ensure_ascii=False), encoding="utf-8"
    )
    # editor.html 必须存在，服务会读它
    editor = tmp_path / "editor.html"
    editor.write_text("<html><body>x</body></html>", encoding="utf-8")

    class _Cfg:
        output_dir = out_dir
        editor_path = editor

    monkeypatch.setattr(mod, "C", _Cfg())

    srv = ThreadingHTTPServer(("127.0.0.1", 0), mod._EditorHandler)
    port = srv.server_address[1]
    import threading

    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}"
        # 1) 读取 events
        with urllib.request.urlopen(base + "/api/events") as r:
            got = json.loads(r.read())
        assert got == events_src
        # 2) 修改后保存
        new_data = [
            {"time": 0.5, "note": 67, "key": "B", "action": "press", "duration": 0.3},
        ]
        req = urllib.request.Request(
            base + "/api/save-events",
            data=json.dumps(new_data).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            resp = json.loads(r.read())
        assert resp["ok"] is True and resp["count"] == 1
        saved = json.loads((out_dir / "events.json").read_text(encoding="utf-8"))
        assert saved[0]["duration"] == 0.3
        # 3) 编辑器页面可访问
        with urllib.request.urlopen(base + "/editor") as r:
            assert b"x" in r.read()
    finally:
        srv.shutdown()
        srv.server_close()
