from argparse import Namespace
import json
from pathlib import Path

from ws50_climate import cli
from ws50_climate.models import Device, Point
from ws50_climate.withings import WithingsRequestError


def test_no_argument_mode_prints_help(capsys):
    assert cli.main([]) == 0
    assert "--once" in capsys.readouterr().out


def test_state_file_defaults_to_working_directory_or_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WS50_CLIMATE_STATE_DIR", raising=False)
    assert cli.parser().parse_args(["--once"]).state_file == tmp_path / "state/ws50-climate.json"
    monkeypatch.setenv("WS50_CLIMATE_STATE_DIR", "/state")
    assert cli.parser().parse_args(["--once"]).state_file == Path("/state/ws50-climate.json")


def test_repeat_interval_defaults_to_one_hour():
    assert cli.parser().parse_args(["--repeat"]).interval_seconds == 3600


def test_pass_pages_forward_from_influx_watermark(monkeypatch, tmp_path):
    monkeypatch.setenv("WITHINGS_EMAIL", "email")
    monkeypatch.setenv("WITHINGS_PASSWORD", "password")
    monkeypatch.setenv("INFLUXDB_URL", "http://influx")
    monkeypatch.setenv("INFLUXDB_USERNAME", "user")
    monkeypatch.setenv("INFLUXDB_PASSWORD", "password")
    monkeypatch.setenv("INFLUXDB_DATABASE", "db")
    written = []

    class FakeWithings:
        def __init__(self, *args, **kwargs): pass
        def devices(self): return [Device("u", "d", "serial")]
        def measurements(self, device, start, end): return [Point("temp", device.serial, 20, start)]

    class FakeInflux:
        def __init__(self, *args): pass
        def write(self, points): written.extend(points)
        def latest_timestamp(self, serial, backfill_days): return None

    monkeypatch.setattr(cli, "WithingsClient", FakeWithings)
    monkeypatch.setattr(cli, "InfluxV1", FakeInflux)
    args = Namespace(state_file=tmp_path / "cache.json", backfill_days=1, page_hours=12)
    assert cli.run_pass(args, now=lambda: 100_000) == (1, 2)
    assert [point.timestamp for point in written] == [13_600, 56_800]
    assert not (tmp_path / "cache.json").exists()


def test_pass_removes_legacy_cursor_and_starts_after_influx_watermark(monkeypatch, tmp_path):
    monkeypatch.setenv("WITHINGS_EMAIL", "email")
    monkeypatch.setenv("WITHINGS_PASSWORD", "password")
    monkeypatch.setenv("INFLUXDB_URL", "http://influx")
    monkeypatch.setenv("INFLUXDB_USERNAME", "user")
    monkeypatch.setenv("INFLUXDB_PASSWORD", "password")
    monkeypatch.setenv("INFLUXDB_DATABASE", "db")
    state_file = tmp_path / "cache.json"
    state_file.write_text('{"cursor": 1}')
    starts = []

    class FakeWithings:
        def __init__(self, *args, **kwargs): pass
        def devices(self): return [Device("u", "d", "serial")]
        def measurements(self, device, start, end): starts.append(start); return []

    class FakeInflux:
        def __init__(self, *args): pass
        def latest_timestamp(self, serial, backfill_days): return 9_000
        def write(self, points): pass

    monkeypatch.setattr(cli, "WithingsClient", FakeWithings)
    monkeypatch.setattr(cli, "InfluxV1", FakeInflux)
    args = Namespace(state_file=state_file, backfill_days=1, page_hours=12)
    cli.run_pass(args, now=lambda: 10_000)
    assert starts == [9_001]
    assert "cursor" not in json.loads(state_file.read_text())


def test_pass_skips_inaccessible_association(monkeypatch, tmp_path):
    monkeypatch.setenv("WITHINGS_EMAIL", "email")
    monkeypatch.setenv("WITHINGS_PASSWORD", "password")
    monkeypatch.setenv("INFLUXDB_URL", "http://influx")
    monkeypatch.setenv("INFLUXDB_USERNAME", "user")
    monkeypatch.setenv("INFLUXDB_PASSWORD", "password")
    monkeypatch.setenv("INFLUXDB_DATABASE", "db")

    class FakeWithings:
        def __init__(self, *args, **kwargs): pass
        def devices(self): return [Device("bad", "d", "serial"), Device("good", "d", "serial")]
        def measurements(self, device, start, end):
            if device.user_id == "bad":
                raise WithingsRequestError(214)
            return [Point("co2", device.serial, 800, start)]

    class FakeInflux:
        def __init__(self, *args): pass
        def write(self, points): self.points = points
        def latest_timestamp(self, serial, backfill_days): return 9_000

    monkeypatch.setattr(cli, "WithingsClient", FakeWithings)
    monkeypatch.setattr(cli, "InfluxV1", FakeInflux)
    args = Namespace(state_file=tmp_path / "cache.json", backfill_days=1, page_hours=24)
    assert cli.run_pass(args, now=lambda: 10_000) == (2, 1)
