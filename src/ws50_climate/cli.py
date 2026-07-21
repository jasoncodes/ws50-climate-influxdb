from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from .influx import InfluxV1
from .state import State
from .withings import WithingsClient, WithingsRequestError


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import Withings WS-50 temperature and CO2 history into InfluxDB v1.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="run one discovery and ingestion pass")
    mode.add_argument("--repeat", action="store_true", help="run continuously")
    parser.add_argument("--interval-seconds", type=int, default=3600, help="repeat interval (default: 3600; one hour)")
    parser.add_argument("--backfill-days", type=int, default=7, help="initial backward discovery period (default: 7)")
    parser.add_argument("--page-hours", type=float, default=6, help="forward pagination page size (default: 6)")
    state_dir = Path(os.getenv("WS50_CLIMATE_STATE_DIR", Path.cwd() / "state"))
    parser.add_argument("--state-file", type=Path, default=state_dir / "ws50-climate.json", help="persistent Withings session cache")
    parser.add_argument("--debug", action="store_true", help="log redacted Withings HTTP request/response details to stderr")
    return parser


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} must be set")
    return value


def configure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(line_buffering=True, write_through=True)


def run_pass(args: argparse.Namespace, now=time.time) -> tuple[int, int]:
    state = State(args.state_file)
    state.load()
    # Migrate caches created by versions that tracked an ingestion cursor.
    if state.data.pop("cursor", None) is not None:
        state.save()
    client = WithingsClient(required_env("WITHINGS_EMAIL"), required_env("WITHINGS_PASSWORD"), state, clock=now, debug=getattr(args, "debug", False))
    influx = InfluxV1(required_env("INFLUXDB_URL"), required_env("INFLUXDB_USERNAME"), required_env("INFLUXDB_PASSWORD"), required_env("INFLUXDB_DATABASE"))
    current = int(now())
    devices = client.devices()
    point_count = 0
    for device in devices:
        watermark = influx.latest_timestamp(device.serial, args.backfill_days)
        start = watermark + 1 if watermark is not None else current - int(args.backfill_days * 86400)
        try:
            while start < current:
                end = min(start + int(args.page_hours * 3600), current)
                points = client.measurements(device, start, end)
                influx.write(points)
                point_count += len(points)
                start = end
        except WithingsRequestError as exc:
            if exc.status != 214:
                raise
            if getattr(args, "debug", False):
                print(f"skipping inaccessible device/user association for serial {device.serial}", file=sys.stderr)
    return len(devices), point_count


def main(argv: list[str] | None = None) -> int:
    configure_output()
    arg_parser = parser()
    if argv is None:
        import sys
        argv = sys.argv[1:]
    if not argv:
        arg_parser.print_help()
        return 0
    args = arg_parser.parse_args(argv)
    while True:
        devices, points = run_pass(args)
        print(f"processed {devices} devices and wrote {points} points")
        if args.once:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
