"""Resumable checkpoint store. Tracks URLs already processed.

Production note: a real prod system would use Redis or Postgres for
distributed coordination. JSON file is sufficient for single-process POC
and trivially upgradeable.
"""
from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Set


class CheckpointStore:
    """Append-only set of URLs already successfully processed."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = Lock()
        self._seen: Set[str] = self._load()

    def _load(self) -> Set[str]:
        if not self.path.exists():
            return set()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return set(data) if isinstance(data, list) else set()
        except (json.JSONDecodeError, OSError):
            return set()

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: tmp → rename
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(self._seen)), encoding="utf-8")
        tmp.replace(self.path)

    def is_seen(self, url: str) -> bool:
        with self._lock:
            return url in self._seen

    def mark_seen(self, url: str) -> None:
        with self._lock:
            if url in self._seen:
                return
            self._seen.add(url)
            self._flush()

    def __len__(self) -> int:
        return len(self._seen)
