"""网页 GUI 测试（Flask test client）。"""
import io

import pretty_midi
import pytest

pytest.importorskip("flask")

import app as webapp

CORNERS = [[100, 100], [800, 100], [800, 400], [100, 400]]


@pytest.fixture
def client(tmp_path):
    # 把 app 的全局配置指向临时目录，避免污染项目 data/
    webapp.C.data_dir = tmp_path
    webapp.C.input_dir = tmp_path
    webapp.C.output_dir = tmp_path
    webapp.C.calibration_path = tmp_path / "calibration.json"
    webapp.C.debug_image = tmp_path / "debug.png"
    webapp.app.testing = True
    return webapp.app.test_client()


def _make_midi_bytes():
    pm = pretty_midi.PrettyMIDI()
    inst = pretty_midi.Instrument(program=0, name="t")
    for p, s in [(60, 0.0), (62, 0.5), (64, 1.0)]:
        inst.notes.append(pretty_midi.Note(velocity=100, pitch=p, start=s, end=s + 0.4))
    pm.instruments.append(inst)
    buf = io.BytesIO()
    pm.write(buf)
    return buf.getvalue()


def test_index(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "风物之诗琴" in r.get_data(as_text=True)


def test_editor(client):
    r = client.get("/editor")
    assert r.status_code == 200
    assert "音符编辑器" in r.get_data(as_text=True)


def test_map_and_events(client):
    data = _make_midi_bytes()
    r = client.post(
        "/api/map",
        data={"midi": (io.BytesIO(data), "s.mid")},
        content_type="multipart/form-data",
    )
    j = r.get_json()
    assert j["ok"], j
    assert j["stats"]["total"] == 3
    assert "/editor?events=/api/events" in j["editor"]
    r2 = client.get("/api/events")
    assert r2.status_code == 200


def test_calibrate(client):
    r = client.post("/api/calibrate", json={"corners": CORNERS, "image_size": [900, 500]})
    j = r.get_json()
    assert j["ok"], j
    assert j["keys"] == 21
    assert client.get("/api/calibration").status_code == 200


def test_play_status_idle(client):
    r = client.get("/api/play/status")
    assert r.get_json()["running"] is False
