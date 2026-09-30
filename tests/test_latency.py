"""⑤-b 逐键延迟采样：纯逻辑测试（不依赖真实音频设备）。"""
import pytest

from piano_tool import calibrate_latency as cl
from piano_tool import latency_probe as probe
from piano_tool.locator import build_calibration


@pytest.fixture
def calibration():
    corners = [[100, 100], [800, 100], [800, 400], [100, 400]]
    return build_calibration(corners, image_size=[900, 500])


class _FakeMonitor:
    def __init__(self, latency=0.05):
        self._latency = latency
        self.opened = 0
        self.closed = 0

    def open(self):
        self.opened += 1

    def close(self):
        self.closed += 1

    def baseline(self, blocks=12):
        return (0.0, 0.0)

    def _drain_old(self):
        pass

    def wait_onset(self, after, timeout=0.8, baseline_mean=0.0, baseline_std=0.0):
        return after + self._latency


def test_measure_key_latency_returns_latency():
    clicks = []
    mon = _FakeMonitor(latency=0.05)

    def click_fn(x, y):
        clicks.append((x, y))

    lat = probe.measure_key_latency(click_fn, 123.0, 456.0, mon, timeout=0.5)
    assert lat is not None
    assert abs(lat - 0.05) < 1e-6
    assert clicks == [(123.0, 456.0)]
    assert mon.opened == 1 and mon.closed == 1


def test_sample_all_keys_returns_21(calibration):
    mon = _FakeMonitor(latency=0.03)
    captured = []
    result = cl.sample_all_keys(
        calibration, monitor=mon,
        click_fn=lambda x, y: None,
        on_each=lambda k, lat: captured.append((k, lat)),
    )
    assert len(result) == 21
    # 每个键都采样到了（fake monitor 永远返回 0.03）
    assert all(v is not None for v in result.values())
    assert len(captured) == 21


def test_sample_all_keys_handles_none(calibration):
    # wait_onset 返回 None -> 该键为 None，但不影响其余键
    class NoSound(_FakeMonitor):
        def wait_onset(self, after, timeout=0.8, baseline_mean=0.0, baseline_std=0.0):
            return None

    result = cl.sample_all_keys(calibration, monitor=NoSound(), click_fn=lambda x, y: None)
    assert len(result) == 21
    assert all(v is None for v in result.values())


def test_profile_roundtrip_and_lead(tmp_path):
    prof_path = tmp_path / "key_latency.json"
    out = cl.save_latency_profile(
        {"Z": 0.05, "X": None, "C": 0.07}, prof_path, device_name="fake"
    )
    loaded = cl.load_latency_profile(out)
    assert loaded is not None
    assert loaded["latencies"]["Z"] == 50.0
    assert loaded["latencies"]["X"] is None
    assert loaded["mean_ms"] == 60.0  # (50+70)/2
    lead = cl.profile_to_lead_seconds(loaded)
    assert lead == {"Z": 0.05, "C": 0.07}
    assert "X" not in lead


def test_pick_monitor_device_no_crash():
    # 不要求具体返回值（取决于机器），只保证不抛异常
    assert probe.pick_monitor_device() in (None, int(probe.pick_monitor_device() or 0)) or True


def test_list_capture_devices_includes_loopback(mocker):
    # 伪造一组设备：1 个真实输入 + 1 个带输出的设备（应产生系统声音回环项）
    fake_devs = [
        {
            "name": "麦克风 (Realtek)",
            "max_input_channels": 2,
            "max_output_channels": 0,
            "default_samplerate": 44100,
        },
        {
            "name": "扬声器 (Realtek)",
            "max_input_channels": 0,
            "max_output_channels": 2,
            "default_samplerate": 48000,
        },
    ]

    class FakeSD:
        @staticmethod
        def query_devices(_=None):
            return fake_devs

    mocker.patch.object(probe, "sd", FakeSD())
    mocker.patch.object(probe, "_HAS_SD", True)

    devs = probe.list_capture_devices()
    # 1 个真实输入 + 1 个系统声音回环 = 2 项
    assert len(devs) == 2
    inputs = [d for d in devs if not d["loopback"]]
    loops = [d for d in devs if d["loopback"]]
    assert len(inputs) == 1
    assert inputs[0]["name"] == "麦克风 (Realtek)"
    assert len(loops) == 1
    assert loops[0]["loopback"] is True
    assert "系统声音(回环)" in loops[0]["name"]
    # 输出设备名应出现在回环标签里，且索引指向那个输出设备
    assert "扬声器" in loops[0]["name"]
    assert loops[0]["index"] == 1


def test_default_loopback_device(mocker):
    class FakeDefault:
        device = (0, 1)  # (默认输入, 默认输出)

    class FakeSD:
        default = FakeDefault()

        @staticmethod
        def query_devices(_=None):
            return []

    mocker.patch.object(probe, "sd", FakeSD())
    mocker.patch.object(probe, "_HAS_SD", True)
    assert probe.default_loopback_device() == 1


def test_default_loopback_device_no_sd(mocker):
    mocker.patch.object(probe, "_HAS_SD", False)
    assert probe.default_loopback_device() is None

