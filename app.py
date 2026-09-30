"""风物之诗琴工具 · 网页 GUI（本地 Flask 服务）

把整套流水线封装成网页：上传音频/ MIDI、可视化微调、四点校准、模拟播放。
仅在本机运行（127.0.0.1），不连接游戏本体。

启动：
    python app.py
    # 浏览器自动打开 http://127.0.0.1:8000
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from flask import (
    Flask,
    Response,
    jsonify,
    request,
    render_template,
    send_file,
)

from piano_tool.config import get_config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("piano_tool.webui")

app = Flask(
    __name__,
    template_folder=str(Path(__file__).parent / "webui" / "templates"),
)
C = get_config()

_play_lock = threading.Lock()
_play_status = {"running": False, "message": ""}


def _play_worker(events_path: Path, dry_run: bool, speed: float, delay_ms: int):
    global _play_status
    try:
        from piano_tool.player import load_events, load_calibration, play

        events = load_events(events_path)
        cal = load_calibration(C.calibration_path)
        play(
            events,
            cal,
            dry_run=dry_run,
            speed=speed,
            delay_ms=delay_ms,
        )
        _play_status["message"] = "播放完成"
    except Exception as exc:  # noqa: BLE001
        logger.exception("play failed")
        _play_status["message"] = f"错误：{exc}"
    finally:
        _play_status["running"] = False


# ----------------------------------------------------------------------------
# 页面
# ----------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/editor")
def editor():
    """返回可视化编辑器（支持 ?events= 自动加载琴谱）。"""
    html = Path(__file__).parent / "editor" / "editor.html"
    return Response(html.read_text(encoding="utf-8"), mimetype="text/html")


# ----------------------------------------------------------------------------
# 数据下载
# ----------------------------------------------------------------------------
@app.route("/api/events")
def get_events():
    p = C.output_dir / "events.json"
    if not p.exists():
        return jsonify({"error": "尚未生成 events.json，请先完成「映射」步骤"}), 404
    return send_file(str(p), mimetype="application/json", as_attachment=False)


@app.route("/download/<kind>")
def download(kind: str):
    if kind == "midi":
        p = C.output_dir / "song.mid"
        if not p.exists():
            return jsonify({"error": "尚未生成 MIDI"}), 404
        return send_file(str(p), as_attachment=True)
    if kind == "events":
        p = C.output_dir / "events.json"
        if not p.exists():
            return jsonify({"error": "尚未生成 events.json"}), 404
        return send_file(str(p), as_attachment=True, download_name="events.json")
    return jsonify({"error": "unknown kind"}), 400


# ----------------------------------------------------------------------------
# ① 音频 → MIDI
# ----------------------------------------------------------------------------
@app.route("/api/convert", methods=["POST"])
def api_convert():
    if "audio" not in request.files:
        return jsonify({"error": "未收到音频文件"}), 400
    f = request.files["audio"]
    if not f.filename:
        return jsonify({"error": "文件名为空"}), 400
    C.input_dir.mkdir(parents=True, exist_ok=True)
    src = C.input_dir / f.filename
    f.save(str(src))

    try:
        from piano_tool.audio_to_midi import audio_to_midi
    except ImportError:
        return jsonify(
            {"error": "未安装 basic-pitch，请先 pip install basic-pitch tensorflow-cpu"}
        ), 500

    out = C.output_dir / "song.mid"
    try:
        count, _ = audio_to_midi(src, out)
    except Exception as exc:  # noqa: BLE001
        logger.exception("convert failed")
        return jsonify({"error": f"转写失败：{exc}"}), 500
    return jsonify({"ok": True, "count": count, "midi": "/download/midi"})


# ----------------------------------------------------------------------------
# ② MIDI → events.json
# ----------------------------------------------------------------------------
@app.route("/api/map", methods=["POST"])
def api_map():
    if "midi" not in request.files:
        return jsonify({"error": "未收到 MIDI 文件"}), 400
    f = request.files["midi"]
    if not f.filename:
        return jsonify({"error": "文件名为空"}), 400
    C.input_dir.mkdir(parents=True, exist_ok=True)
    src = C.input_dir / f.filename
    f.save(str(src))

    try:
        from piano_tool.midi_mapper import map_midi_to_events, write_events

        events, stats = map_midi_to_events(src)
        out = write_events(events, C.output_dir / "events.json")
    except Exception as exc:  # noqa: BLE001
        logger.exception("map failed")
        return jsonify({"error": f"映射失败：{exc}"}), 500
    return jsonify(
        {
            "ok": True,
            "stats": stats,
            "events": "/api/events",
            "editor": "/editor?events=/api/events",
        }
    )


@app.route("/api/upload-events", methods=["POST"])
def api_upload_events():
    if "events" not in request.files:
        return jsonify({"error": "未收到 events.json"}), 400
    f = request.files["events"]
    try:
        data = json.loads(f.read().decode("utf-8"))
        assert isinstance(data, list)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"events.json 解析失败：{exc}"}), 400
    out = write_events_upload(data)
    return jsonify({"ok": True, "events": "/api/events",
                    "editor": "/editor?events=/api/events", "count": len(data)})


def write_events_upload(data):
    from piano_tool.midi_mapper import build_events_json

    out = C.output_dir / "events.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(build_events_json_from(data), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return out


def build_events_json_from(data):
    return [
        {
            "time": round(float(n.get("time", 0)), 3),
            "note": int(n.get("note", 60)),
            "key": str(n.get("key", "")),
            "action": str(n.get("action", "press")),
        }
        for n in data
    ]


# ----------------------------------------------------------------------------
# ④ 校准（浏览器：上传截图 + 点击四角）
# ----------------------------------------------------------------------------
@app.route("/api/upload-screenshot", methods=["POST"])
def api_upload_screenshot():
    if "screenshot" not in request.files:
        return jsonify({"error": "未收到截图"}), 400
    f = request.files["screenshot"]
    C.data_dir.mkdir(parents=True, exist_ok=True)
    dst = C.data_dir / "screenshot_upload.png"
    f.save(str(dst))
    return jsonify({"ok": True, "url": "/api/screenshot"})


@app.route("/api/screenshot")
def get_screenshot():
    p = C.data_dir / "screenshot_upload.png"
    if not p.exists():
        return jsonify({"error": "尚未上传截图"}), 404
    return send_file(str(p))


@app.route("/api/calibrate", methods=["POST"])
def api_calibrate():
    body = request.get_json(force=True, silent=True) or {}
    corners = body.get("corners")
    if not corners or len(corners) != 4:
        return jsonify({"error": "corners 必须是 4 个点 [[x,y],...]"}), 400
    image_size = body.get("image_size")

    try:
        from piano_tool.locator import build_calibration, draw_debug_image

        cal = build_calibration(corners, image_size=image_size)
        save_calibration(cal)
    except Exception as exc:  # noqa: BLE001
        logger.exception("calibrate failed")
        return jsonify({"error": f"校准失败：{exc}"}), 500

    # 若上传过截图，生成可视化调试图
    shot = C.data_dir / "screenshot_upload.png"
    if shot.exists():
        try:
            draw_debug_image(str(shot), cal, C.debug_image)
        except Exception as exc:  # noqa: BLE001
            logger.warning("debug image 生成失败：%s", exc)

    return jsonify(
        {
            "ok": True,
            "keys": len(cal.keys),
            "calibration": "/api/calibration",
            "debug": "/api/debug-image" if C.debug_image.exists() else None,
        }
    )


def save_calibration(cal):
    from piano_tool.locator import save_calibration as _save

    _save(cal, C.calibration_path)


@app.route("/api/calibration")
def get_calibration():
    p = C.calibration_path
    if not p.exists():
        return jsonify({"error": "尚未校准"}), 404
    return send_file(str(p), mimetype="application/json")


@app.route("/api/debug-image")
def get_debug_image():
    p = C.debug_image
    if not p.exists():
        return jsonify({"error": "尚未生成调试图"}), 404
    return send_file(str(p))


# ----------------------------------------------------------------------------
# ⑤ 播放
# ----------------------------------------------------------------------------
@app.route("/api/play", methods=["POST"])
def api_play():
    global _play_status
    body = request.get_json(force=True, silent=True) or {}
    dry_run = bool(body.get("dry_run", False))
    speed = float(body.get("speed", 1.0))
    delay_ms = int(body.get("delay_ms", 20))

    events_path = C.output_dir / "events.json"
    if not events_path.exists():
        return jsonify({"error": "尚未生成 events.json"}), 400
    if not C.calibration_path.exists():
        return jsonify({"error": "尚未校准，请先完成「校准」步骤"}), 400

    with _play_lock:
        if _play_status["running"]:
            return jsonify({"error": "正在播放中，请稍候"}), 409
        _play_status["running"] = True
        _play_status["message"] = "播放中…"
        t = threading.Thread(
            target=_play_worker,
            args=(events_path, dry_run, speed, delay_ms),
            daemon=True,
        )
        t.start()
    return jsonify(
        {"ok": True, "dry_run": dry_run, "message": "已启动（真实点击请在模拟器页面保持可见）"}
    )


@app.route("/api/play/status")
def play_status():
    return jsonify(_play_status)


@app.route("/api/stop", methods=["POST"])
def api_stop():
    # pyautogui FAILSAFE 需用户把鼠标移到左上角；此处仅标记状态
    _play_status["running"] = False
    _play_status["message"] = "已请求停止"
    return jsonify({"ok": True})


def main():
    import webbrowser

    url = "http://127.0.0.1:8000"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=8000, debug=False)


if __name__ == "__main__":
    main()
