from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class CacheStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"quotes": {}, "fx": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"quotes": {}, "fx": {}}

    def save(self, data: dict[str, Any]) -> None:
        payload = json.dumps(data, indent=2)
        self.path.write_text(payload, encoding="utf-8")
