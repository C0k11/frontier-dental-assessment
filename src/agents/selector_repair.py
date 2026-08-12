"""SelectorRepair: when a selector fails N times in a row, ask the LLM
to suggest a replacement based on observed HTML.

This is the "agentic adaptation" win. Real production scrapers fail when
sites change their CSS class names. A repair loop closes the gap.

Usage pattern (called from extractor on extraction failure):
  - track per-field failure count
  - when count exceeds threshold, invoke this agent
  - candidate selectors are written to a `selector_overrides.yaml`
  - extractor picks them up on the next run

For the POC we don't auto-deploy the repaired selector - we surface
it in logs + a JSON file for human review. That's a deliberate
production-safety choice (auto-deploying LLM-generated CSS is risky).
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from threading import Lock

from loguru import logger

from ..llm import LLMClient


class SelectorRepairAgent:
    def __init__(
        self,
        llm: LLMClient,
        failure_threshold: int = 5,
        suggestions_path: str | Path = "data/output/selector_suggestions.json",
        enabled: bool = True,
    ):
        self.llm = llm
        self.threshold = failure_threshold
        self.path = Path(suggestions_path)
        self.enabled = enabled
        self._failures: dict[str, int] = defaultdict(int)
        self._suggested: dict[str, list[str]] = {}
        self._lock = Lock()

    def record_failure(self, field: str) -> bool:
        """Returns True if threshold reached (caller should invoke repair)."""
        with self._lock:
            self._failures[field] += 1
            return self._failures[field] >= self.threshold and field not in self._suggested

    async def repair(self, field: str, broken_selector: str, html: str) -> list[str]:
        """Ask LLM for replacement selectors. Persist suggestions."""
        if not self.enabled:
            return []
        try:
            candidates = await self.llm.suggest_selectors(html, broken_selector, field)
        except Exception as e:
            logger.error(f"SelectorRepair LLM call failed for {field}: {e}")
            return []

        if candidates:
            with self._lock:
                self._suggested[field] = candidates
                self._persist()
            logger.warning(
                f"SelectorRepair: field='{field}' broken='{broken_selector}' "
                f"suggested={candidates} (review before deploying)"
            )
        return candidates

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "failures": dict(self._failures),
            "suggested_selectors": self._suggested,
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
