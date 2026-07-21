from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Device:
    user_id: str
    device_id: str
    serial: str


@dataclass(frozen=True)
class Point:
    measurement: str
    serial: str
    value: float
    timestamp: int

