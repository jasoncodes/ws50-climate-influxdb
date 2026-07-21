from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class State:
    """Small JSON cache.  It deliberately contains no account password."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, Any] = {}

    def load(self) -> None:
        try:
            self.data = json.loads(self.path.read_text())
        except FileNotFoundError:
            self.data = {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid state cache {self.path}: {exc}") from exc

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, sort_keys=True) + "\n")
        temporary.replace(self.path)

