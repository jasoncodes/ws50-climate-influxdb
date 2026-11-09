import json

from ws50_climate.models import Device
from ws50_climate.state import State
from ws50_climate.withings import WithingsClient


def response(payload, headers=None):
    return (headers or {}, json.dumps(payload).encode())


def test_login_is_cached_until_expiry(tmp_path):
    calls = []

    def opener(request):
        calls.append((request.full_url, request.get_header("Cookie"), request.get_header("Content-type"), request.data))
        return response({}, {"Set-Cookie": "session_key=key; Path=/; Expires=Thu, 01 Jan 1970 00:33:20 GMT"})

    state = State(tmp_path / "state.json")
    state.load()
    client = WithingsClient("a@example.test", "secret", state, opener=opener, clock=lambda: 1000)
    assert client.session_key() == "key"
    assert client.session_key() == "key"
    assert calls == [
        (
            "https://account.withings.com/new_workflow/password_check",
            "email=a%40example.test",
            "application/x-www-form-urlencoded",
            b"password=secret",
        )
    ]
    assert "secret" not in state.path.read_text()


def test_debug_output_redacts_password_and_session(tmp_path, capsys):
    def opener(request):
        return response({}, {"Set-Cookie": "session_key=live-session; Path=/"})

    state = State(tmp_path / "state.json")
    state.load()
    client = WithingsClient("a@example.test", "secret", state, opener=opener, debug=True)
    client._debug(f"body={client._redact_body(b'password=secret&session_key=live-session')}")
    output = capsys.readouterr().err
    assert "secret" not in output
    assert "live-session" not in output
    assert "<redacted>" in output


def test_session_cookie_uses_aggregated_canonical_header():
    headers = {
        "set-cookie": "analytics_cookie=ignored; Path=/",
        "Set-Cookie": "analytics_cookie=ignored; Path=/\nsession_key=key; Path=/",
    }
    assert WithingsClient._session_from_cookies(headers)[0] == "key"


def test_deleted_session_cookie_is_not_used():
    headers = {"Set-Cookie": "session_key=deleted; Path=/; Max-Age=0"}
    assert WithingsClient._session_from_cookies(headers)[0] is None


def test_unauthorized_api_session_is_refreshed_once(tmp_path):
    calls = []

    def opener(request):
        cookie = request.get_header("Cookie")
        calls.append((request.full_url, cookie))
        if "password_check" in request.full_url:
            return response({}, {"Set-Cookie": "session_key=fresh; Path=/"})
        if cookie == "session_key=stale":
            return response({"status": 2555})
        return response({"body": {"users": [{"userid": 7, "devices": [{"deviceid": 8, "serial": "WS50"}]}]}})

    state = State(tmp_path / "state.json")
    state.load()
    state.data["session"] = {"key": "stale", "expires_at": 9999999999}
    client = WithingsClient("a", "b", state, opener=opener, clock=lambda: 1000)

    assert client.devices() == [Device("7", "8", "WS50")]
    assert calls == [
        ("https://scalews.withings.com/cgi-bin/association", "session_key=stale"),
        ("https://account.withings.com/new_workflow/password_check", "email=a"),
        ("https://scalews.withings.com/cgi-bin/association", "session_key=fresh"),
    ]


def test_association_schema_resolves_accounts_to_device_users(tmp_path):
    def opener(request):
        if "password_check" in request.full_url:
            return response({}, {"Set-Cookie": "session_key=key; Path=/"})
        return response({"body": {
            "accounts": [{"accountid": 7, "users": [{"userid": 9}]}],
            "associations": [{"deviceid": 8, "accounts": [7], "deviceproperties": {"sn": "WS50"}}],
        }})

    state = State(tmp_path / "state.json")
    state.load()
    client = WithingsClient("a", "b", state, opener=opener)
    assert client.devices() == [Device("9", "8", "WS50")]


def test_current_series_shape_decodes_temperature_and_co2(tmp_path):
    def opener(request):
        if "password_check" in request.full_url:
            return response({}, {"Set-Cookie": "session_key=key; Path=/"})
        return response({"body": {"series": [
            {"type": 35, "data": [{"date": 100, "value": 800}]},
            {"type": 12, "data": [{"date": 100, "value": 21.5}]},
        ]}})

    state = State(tmp_path / "state.json")
    state.load()
    client = WithingsClient("a", "b", state, opener=opener)
    points = client.measurements(Device("u", "d", "WS50"), 1, 2)
    assert {(point.measurement, point.value) for point in points} == {("co2", 800), ("temp", 21.5)}


def test_association_discovers_device_and_measurements(tmp_path):
    def opener(request):
        if "password_check" in request.full_url:
            return response({"body": {"session_key": "key", "expires_at": 9999999999}})
        if "association" in request.full_url:
            assert b"appliver=a5ae62e5" in request.data
            return response({"body": {"users": [{"userid": 7, "devices": [{"deviceid": 8, "serial": "WS50 A"}]}]}})
        assert b"appliver=a5ae62e5" in request.data
        return response({"body": {"series": [{"date": 100, "measures": [
            {"type": 12, "value": 215, "unit": -1}, {"type": 35, "value": 700, "unit": 0}, {"type": 1, "value": 1}
        ]}]}})

    state = State(tmp_path / "state.json")
    state.load()
    client = WithingsClient("a", "b", state, opener=opener, clock=lambda: 1000)
    devices = client.devices()
    assert devices == [Device("7", "8", "WS50 A")]
    assert {(p.measurement, p.value, p.timestamp) for p in client.measurements(devices[0], 1, 2)} == {("temp", 21.5, 100), ("co2", 700, 100)}
