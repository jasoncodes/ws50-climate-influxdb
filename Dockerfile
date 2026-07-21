FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN uv sync --no-dev

FROM python:3.12-slim-bookworm
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/src /app/src
RUN useradd --system --uid 10001 importer && mkdir /state && chown importer:importer /state
USER importer
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/src WS50_CLIMATE_STATE_DIR=/state
ENTRYPOINT ["ws50-climate-influxdb"]
CMD ["--repeat"]
