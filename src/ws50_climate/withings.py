from __future__ import annotations

import json
import re
import sys
import time
from email.utils import parsedate_to_datetime
from http.client import HTTPSConnection
from typing import Any
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from .models import Device, Point
from .state import State

ACCOUNT_URL = "https://account.withings.com/new_workflow/password_check"
ASSOCIATION_URL = "https://scalews.withings.com/cgi-bin/association"
MEASURE_URL = "https://scalews.withings.com/cgi-bin/v2/measure"
TYPE_TO_MEASUREMENT = {12: "temp", 35: "co2"}
APP_FIELDS = {"appname": "hmw", "apppfm": "web", "appliver": "a5ae62e5"}


class WithingsRequestError(RuntimeError):
    def __init__(self, status: int):
        super().__init__(f"Withings request failed with status {status}")
        self.status = status


class WithingsClient:
    def __init__(self, email: str, password: str, state: State, opener=None, clock=time.time, debug: bool = False):
        self.email = email
        self.password = password
        self.state = state
        self.opener = opener or self._open
        self.account_opener = opener or self._account_open
        self.clock = clock
        self.debug = debug

    def _debug(self, message: str) -> None:
        if self.debug:
            print(message, file=sys.stderr)

    @staticmethod
    def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
        return {key: "<redacted>" if key.lower() in {"cookie", "set-cookie", "authorization"} else value for key, value in headers.items()}

    @staticmethod
    def _redact_body(raw: bytes) -> str:
        text = raw.decode("utf-8", "replace")[:2_000]
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            body = payload.get("body")
            body_keys = sorted(body) if isinstance(body, dict) else type(body).__name__
            return f"<JSON keys={sorted(payload)} status={payload.get('status')!r} body_keys={body_keys}>"
        text = re.sub(r"(?i)(password|session_key)(=|\"\s*:\s*\")[^&\"\s<]+", r"\1\2<redacted>", text)
        return text

    def _open(self, request: Request) -> tuple[dict[str, str], bytes]:
        self._debug(f"Withings request: {request.method} {request.full_url} headers={self._redact_headers(dict(request.header_items()))} body={self._redact_body(request.data or b'')}")
        with urlopen(request, timeout=30) as response:  # noqa: S310 - configured HTTPS URL
            headers = dict(response.headers.items())
            # ``dict(headers.items())`` keeps only the final Set-Cookie line.
            # Preserve all of them so the login session can be extracted.
            headers["Set-Cookie"] = "\n".join(response.headers.get_all("Set-Cookie") or [])
            headers[":status"] = str(response.status)
            raw = response.read()
            self._debug(f"Withings response: status={response.status} headers={self._redact_headers(headers)} body={self._redact_body(raw)}")
            return headers, raw

    def _account_open(self, request: Request) -> tuple[dict[str, str], bytes]:
        """Issue the account request with curl-equivalent HTTP headers.

        urllib adds ``Accept-Encoding: identity`` and a Python user agent. The
        account workflow instead accepts the compact request emitted by curl.
        This remains a Python HTTPS connection; no external process is used.
        """
        parsed = urlsplit(request.full_url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        data = request.data or b""
        self._debug(f"Withings request: POST {request.full_url} headers={{'User-Agent': 'curl/8.0.0', 'Accept': '*/*', 'Cookie': '<redacted>', 'Content-Type': 'application/x-www-form-urlencoded'}} body={self._redact_body(data)}")
        connection = HTTPSConnection(parsed.netloc, timeout=30)
        try:
            connection.putrequest("POST", path, skip_accept_encoding=True)
            connection.putheader("User-Agent", "curl/8.0.0")
            connection.putheader("Accept", "*/*")
            connection.putheader("Cookie", request.get_header("Cookie") or "")
            connection.putheader("Content-Type", "application/x-www-form-urlencoded")
            connection.putheader("Content-Length", str(len(data)))
            connection.endheaders(data)
            response = connection.getresponse()
            headers = dict(response.headers.items())
            headers["Set-Cookie"] = "\n".join(response.headers.get_all("Set-Cookie") or [])
            headers[":status"] = str(response.status)
            raw = response.read()
            self._debug(f"Withings response: status={response.status} headers={self._redact_headers(headers)} body={self._redact_body(raw)}")
            return headers, raw
        finally:
            connection.close()

    @staticmethod
    def _header(headers: dict[str, str], name: str) -> str:
        # _open adds an aggregated canonical Set-Cookie value after preserving
        # the server's original header spelling. Prefer that exact key.
        return headers.get(name) or next((value for key, value in headers.items() if key.lower() == name.lower()), "")

    @staticmethod
    def _session_from_cookies(headers: dict[str, str]) -> tuple[str | None, float | None]:
        key: str | None = None
        expires_at: float | None = None
        for line in WithingsClient._header(headers, "Set-Cookie").splitlines():
            # Withings' cookies include attributes unsupported by some Python
            # SimpleCookie versions, so extract just the cookie we need.
            match = re.search(r"(?:^|[;,]\s*)session_key=([^;,\s]+)", line)
            if not match:
                continue
            # A failed/expired login clears the old cookie with
            # ``session_key=deleted; Max-Age=0``.  It is not a credential.
            if re.search(r"(?:^|;\s*)max-age=0(?:;|$)", line, re.IGNORECASE):
                continue
            key = match.group(1)
            expiry = re.search(r"(?:^|;\s*)expires=([^;]+)", line, re.IGNORECASE)
            if expiry:
                try:
                    expires_at = parsedate_to_datetime(expiry.group(1)).timestamp()
                except (TypeError, ValueError):
                    pass
        return key, expires_at

    def _request(
        self,
        url: str,
        fields: dict[str, str],
        session_key: str | None = None,
        cookies: dict[str, str] | None = None,
        content_type: str = "text/plain",
        retry_auth: bool = True,
    ) -> dict[str, Any]:
        request = Request(url, data=urlencode(fields).encode(), method="POST")
        request.add_header("Content-Type", content_type)
        cookie_values = cookies.copy() if cookies else {}
        if session_key:
            cookie_values["session_key"] = session_key
        if cookie_values:
            request.add_header("Cookie", "; ".join(f"{name}={quote(value, safe='')}" for name, value in cookie_values.items()))
        headers, raw = self.opener(request)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Withings returned a non-JSON response") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Withings returned an unexpected response")
        if payload.get("status") not in (None, 0):
            status = int(payload["status"])
            if status == 2555 and session_key and retry_auth:
                self.state.data.pop("session", None)
                self.state.save()
                return self._request(url, fields, self.session_key(), cookies, content_type, False)
            raise WithingsRequestError(status)
        return payload

    @staticmethod
    def _find_value(value: Any, names: set[str]) -> Any | None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in names and child not in (None, ""):
                    return child
            for child in value.values():
                found = WithingsClient._find_value(child, names)
                if found not in (None, ""):
                    return found
        elif isinstance(value, list):
            for child in value:
                found = WithingsClient._find_value(child, names)
                if found not in (None, ""):
                    return found
        return None

    def session_key(self) -> str:
        cache = self.state.data.get("session", {})
        if cache.get("key") and float(cache.get("expires_at", 0)) > self.clock() + 60:
            return str(cache["key"])
        request = Request(ACCOUNT_URL, data=urlencode({"password": self.password}).encode(), method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        request.add_header("Cookie", f"email={quote(self.email, safe='')}")
        headers, raw = self.account_opener(request)
        key, expires_at = self._session_from_cookies(headers)
        if not key:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
            key = self._find_value(payload, {"session_key", "sessionkey"})
        if not key:
            raise RuntimeError("password_check did not return a session_key")
        expires = self._find_value(payload, {"expires_at", "expiration", "expires"}) if "payload" in locals() else None
        try:
            expires_at = expires_at or float(expires)
            if expires_at < self.clock():
                expires_at = self.clock() + expires_at
        except (TypeError, ValueError):
            # Web sessions normally last several days.  A conservative cache avoids repeated logins.
            expires_at = self.clock() + 24 * 60 * 60
        self.state.data["session"] = {"key": str(key), "expires_at": expires_at}
        self.state.save()
        return str(key)

    def devices(self) -> list[Device]:
        key = self.session_key()
        payload = self._request(ASSOCIATION_URL, {"action": "getbyaccountid", "enrich": "t", **APP_FIELDS}, key)
        devices: list[Device] = []
        body = payload.get("body", {})

        # Current association responses map a device's account IDs to users
        # in a separate top-level list, and store the serial as ``sn``.
        # Resolve that explicit relationship before handling older shapes.
        if isinstance(body, dict):
            users_by_account: dict[str, list[str]] = {}
            for account in body.get("accounts", []):
                if not isinstance(account, dict) or account.get("accountid") is None:
                    continue
                users_by_account[str(account["accountid"])] = [
                    str(user["userid"])
                    for user in account.get("users", [])
                    if isinstance(user, dict) and user.get("userid") is not None
                ]
            for association in body.get("associations", []):
                if not isinstance(association, dict):
                    continue
                properties = association.get("deviceproperties", {})
                if not isinstance(properties, dict):
                    properties = {}
                device_id = association.get("deviceid", properties.get("id"))
                serial = properties.get("sn")
                if device_id is None or not serial:
                    continue
                for account_id in association.get("accounts", []):
                    for user_id in users_by_account.get(str(account_id), []):
                        candidate = Device(user_id, str(device_id), str(serial))
                        if candidate not in devices:
                            devices.append(candidate)

        def walk(item: Any, inherited_user: Any = None) -> None:
            if isinstance(item, dict):
                user = item.get("userid", item.get("user_id", inherited_user))
                device_id = item.get("deviceid", item.get("device_id", item.get("id")))
                serial = item.get("serial", item.get("serialnumber", item.get("device_serial")))
                if user is not None and device_id is not None and serial:
                    candidate = Device(str(user), str(device_id), str(serial))
                    if candidate not in devices:
                        devices.append(candidate)
                for child in item.values():
                    walk(child, user)
            elif isinstance(item, list):
                for child in item:
                    walk(child, inherited_user)

        walk(payload)
        if not devices:
            raise RuntimeError("no associated devices with serials were found")
        return devices

    def measurements(self, device: Device, start: int, end: int) -> list[Point]:
        payload = self._request(MEASURE_URL, {
            "action": "getmeashf", "deviceid": device.device_id, "userid": device.user_id,
            "meastype": "35,12", "startdate": str(start), "enddate": str(end),
            **APP_FIELDS,
        }, self.session_key())
        points: list[Point] = []
        body = payload.get("body", {})

        # getmeashf's current response is one series per requested type:
        # {"type": 35, "data": [{"date": ..., "value": ...}]}.
        if isinstance(body, dict):
            for series in body.get("series", []):
                if not isinstance(series, dict):
                    continue
                try:
                    kind = int(series.get("type"))
                except (TypeError, ValueError):
                    continue
                if kind not in TYPE_TO_MEASUREMENT:
                    continue
                for sample in series.get("data", []):
                    if not isinstance(sample, dict):
                        continue
                    try:
                        points.append(Point(TYPE_TO_MEASUREMENT[kind], device.serial, float(sample["value"]), int(sample["date"])))
                    except (KeyError, TypeError, ValueError):
                        continue

        def collect(node: Any, timestamp: int | None = None) -> None:
            if isinstance(node, dict):
                date = node.get("date", node.get("timestamp", timestamp))
                measures = node.get("measures", node.get("measurements"))
                if date is not None and isinstance(measures, list):
                    for measure in measures:
                        if not isinstance(measure, dict):
                            continue
                        kind = measure.get("type", measure.get("meastype"))
                        try:
                            kind = int(kind)
                        except (TypeError, ValueError):
                            continue
                        if kind not in TYPE_TO_MEASUREMENT:
                            continue
                        value = measure.get("value")
                        try:
                            value = float(value) * (10 ** int(measure.get("unit", 0)))
                            points.append(Point(TYPE_TO_MEASUREMENT[kind], device.serial, value, int(date)))
                        except (TypeError, ValueError):
                            continue
                for child in node.values():
                    collect(child, int(date) if date is not None else timestamp)
            elif isinstance(node, list):
                for child in node:
                    collect(child, timestamp)

        collect(body or payload)
        return list(dict.fromkeys(points))
