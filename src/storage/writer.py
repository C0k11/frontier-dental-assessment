"""Output writers. JSONL is the canonical store; CSV is an export view."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from threading import Lock

from ..models import Product


class JsonlWriter:
    """Append-only JSONL writer. One product per line — easy to stream / dedup."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def append(self, product: Product) -> None:
        line = product.model_dump_json()
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def read_all(self) -> list[Product]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                out.append(Product.model_validate_json(line))
        return out


# === CSV export ===

CSV_COLUMNS = [
    "name", "brand", "sku", "price", "currency", "pack_size",
    "availability", "category_path", "url", "description",
    "image_urls", "specifications", "extraction_method",
    "extraction_confidence", "fields_via_llm", "extracted_at",
]


def csv_export(jsonl_path: str | Path, csv_path: str | Path) -> int:
    """Convert JSONL → CSV. Returns row count.

    Lists / dicts are JSON-encoded so the CSV stays one-row-per-product.
    """
    src = Path(jsonl_path)
    dst = Path(csv_path)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if not src.exists():
        return 0

    count = 0
    with open(src, "r", encoding="utf-8") as fin, open(dst, "w", encoding="utf-8", newline="") as fout:
        writer = csv.DictWriter(fout, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            row = {col: _flatten(obj.get(col)) for col in CSV_COLUMNS}
            writer.writerow(row)
            count += 1
    return count


def _flatten(v):
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return v
