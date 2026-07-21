from __future__ import annotations

import base64
import json
from datetime import datetime
from urllib.parse import urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from .models import Point


def line_protocol(points: list[Point]) -> str:
    def escape_tag(value: str) -> str:
        return value.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")
    return "\n".join(f"{point.measurement},serial={escape_tag(point.serial)} value={point.value} {point.timestamp * 1_000_000_000}" for point in points)


class InfluxV1:
    def __init__(self, url: str, username: str, password: str, database: str, opener=None, query_opener=None):
        self.url, self.username, self.password, self.database = url, username, password, database
        self.opener = opener or self._open
        self.query_opener = query_opener or self._query_open

    @staticmethod
    def _open(request: Request) -> int:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - configured service URL
            return response.status

    @staticmethod
    def _query_open(request: Request) -> bytes:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - configured service URL
            return response.read()

    def _authorization(self) -> str:
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return f"Basic {token}"

    def _endpoint(self, path: str, query: dict[str, str]) -> str:
        parsed = urlparse(self.url)
        return urlunparse(parsed._replace(path=parsed.path.rstrip("/") + path, query=urlencode(query)))

    def write(self, points: list[Point]) -> None:
        if not points:
            return
        endpoint = self._endpoint("/write", {"db": self.database, "precision": "ns"})
        request = Request(endpoint, data=line_protocol(points).encode(), method="POST")
        request.add_header("Authorization", self._authorization())
        request.add_header("Content-Type", "text/plain")
        status = self.opener(request)
        if status not in (200, 204):
            raise RuntimeError(f"InfluxDB write returned HTTP {status}")

    def latest_timestamp(self, serial: str, backfill_days: int) -> int | None:
        """Return the older climate watermark, or None when backfill is needed."""
        if backfill_days <= 0:
            raise ValueError("backfill_days must be positive")
        duration = f"{backfill_days // 7}w" if backfill_days % 7 == 0 else f"{backfill_days}d"
        escaped = serial.replace("\\", "\\\\").replace("'", "\\'")
        query = "; ".join(
            f"SELECT LAST(value) FROM {measurement} WHERE serial = '{escaped}' AND time > now() - {duration}"
            for measurement in ("temp", "co2")
        )
        request = Request(self._endpoint("/query", {"db": self.database, "q": query}))
        request.add_header("Authorization", self._authorization())
        try:
            payload = json.loads(self.query_opener(request))
        except json.JSONDecodeError as exc:
            raise RuntimeError("InfluxDB returned a non-JSON query response") from exc
        timestamps: list[int] = []
        for result in payload.get("results", []):
            if result.get("error"):
                raise RuntimeError(f"InfluxDB watermark query failed: {result['error']}")
            for series in result.get("series", []):
                values = series.get("values", [])
                if not values or not values[0]:
                    continue
                try:
                    timestamps.append(int(datetime.fromisoformat(values[0][0].replace("Z", "+00:00")).timestamp()))
                except (AttributeError, TypeError, ValueError):
                    continue
        # Requiring both measurements prevents a missing temp or CO2 series
        # from being permanently skipped after the other one has data.
        return min(timestamps) if len(timestamps) == 2 else None
