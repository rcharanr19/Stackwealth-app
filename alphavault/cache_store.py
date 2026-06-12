from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)


class CacheStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            LOGGER.debug("Cache file %s does not exist; returning empty cache", self.path)
            return {"quotes": {}, "fx": {}}
        try:
            payload = self.path.read_text(encoding="utf-8")
            data = json.loads(payload)
            LOGGER.debug(
                "Loaded cache file %s (%d bytes)",
                self.path,
                len(payload.encode("utf-8")),
            )
            return data
        except json.JSONDecodeError:
            LOGGER.warning("Cache file %s contains invalid JSON; returning empty cache", self.path)
        except OSError:
            LOGGER.exception("Failed to read cache file %s; returning empty cache", self.path)
        return {"quotes": {}, "fx": {}}

    def save(self, data: dict[str, Any]) -> None:
        payload = json.dumps(data, indent=2)
        self.path.write_text(payload, encoding="utf-8")
        LOGGER.debug(
            "Saved cache file %s (%d bytes, %d top-level keys)",
            self.path,
            len(payload.encode("utf-8")),
            len(data),
        )
