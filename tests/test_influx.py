from urllib.parse import unquote_plus

from ws50_climate.influx import InfluxV1, line_protocol
from ws50_climate.models import Point


def test_line_protocol_is_telegraf_style_and_escapes_serial():
    assert line_protocol([Point("temp", "A B,1", 21.5, 10)]) == "temp,serial=A\\ B\\,1 value=21.5 10000000000"


def test_write_uses_v1_endpoint_and_basic_auth():
    seen = {}

    def opener(request):
        seen["url"] = request.full_url
        seen["body"] = request.data.decode()
        seen["auth"] = request.get_header("Authorization")
        return 204

    InfluxV1("http://influx:8086", "user", "pass", "climate", opener).write([Point("co2", "x", 800, 1)])
    assert seen["url"] == "http://influx:8086/write?db=climate&precision=ns"
    assert seen["body"] == "co2,serial=x value=800 1000000000"
    assert seen["auth"].startswith("Basic ")


def test_latest_timestamp_uses_older_measurement_watermark():
    seen = {}

    def query_opener(request):
        seen["url"] = request.full_url
        return b'''{"results":[
            {"series":[{"values":[["2026-07-20T00:00:00Z", 20]]}]},
            {"series":[{"values":[["2026-07-20T01:00:00Z", 800]]}]}
        ]}'''

    influx = InfluxV1("http://influx:8086", "user", "pass", "climate", query_opener=query_opener)
    assert influx.latest_timestamp("WS 50", 7) == 1_784_505_600
    assert "/query?" in seen["url"]
    assert "LAST" in seen["url"]
    assert "time > now() - 1w" in unquote_plus(seen["url"])


def test_latest_timestamp_requires_both_measurements():
    def query_opener(request):
        return b'{"results":[{"series":[{"values":[["2026-07-20T00:00:00Z", 20]]}]},{}]}'

    assert InfluxV1("http://influx", "u", "p", "db", query_opener=query_opener).latest_timestamp("x", 1) is None


def test_latest_timestamp_raises_for_influx_query_error():
    def query_opener(request):
        return b'{"results":[{"error":"database not found"}]}'

    influx = InfluxV1("http://influx", "u", "p", "db", query_opener=query_opener)
    try:
        influx.latest_timestamp("x", 1)
    except RuntimeError as exc:
        assert "database not found" in str(exc)
    else:
        raise AssertionError("expected watermark query failure")
