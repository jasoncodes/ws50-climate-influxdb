# WS-50 climate to InfluxDB

Imports historical Withings WS-50 temperature (type 12) and CO2 (type 35) into
InfluxDB v1. Points use `temp` and `co2` measurements, a `serial` tag, a
`value` field, and the original measurement timestamp.

Create `.env` from `.env.example`, then run with uv:

```sh
uv run ws50-climate-influxdb --once
```

The first pass begins seven days ago and advances one six-hour page per run.
By default its session cache is stored as `./state/ws50-climate.json`; set
`WS50_CLIMATE_STATE_DIR` to choose a directory, or use `--state-file` for an
exact path. The InfluxDB temp/co2 timestamps are the ingestion watermark: a
missing measurement triggers the configured initial backfill, while later runs
continue from the older of the two stored timestamps. Use `--backfill-days`
and `--page-hours` to change those values. InfluxDB v1's timestamp-and-series
upsert behavior makes reruns idempotent.

For a service, create `.env` and use:

```sh
docker compose up -d --build
```

The repeat-mode service runs once per hour by default. Use
`--interval-seconds` to override the cadence when invoking the CLI directly
or by changing the Compose command.

The compose service runs as a non-root user and sets
`WS50_CLIMATE_STATE_DIR=/state`, which maps its session cache to the
named `ws50-climate-state` volume. The cache holds the session key and expiry,
never the account password. Omit command-line arguments to print help.
