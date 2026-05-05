"""CLI entry point.

Usage:
  python -m src.main scrape --config config/targets.yaml
  python -m src.main export --jsonl data/output/products.jsonl --csv data/output/products.csv
  python -m src.main status --checkpoint data/checkpoints/seen.json
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import typer
from dotenv import load_dotenv
from loguru import logger

from .config import load_config
from .orchestrator import Orchestrator
from .storage import csv_export

app = typer.Typer(add_completion=False, help="Frontier Dental Take-Home — agent-based scraper")


def _setup_logging(level: str, log_path: str | Path) -> None:
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stderr, level=level, format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}")
    logger.add(log_path, level=level, rotation="10 MB", retention=3, encoding="utf-8")


@app.command()
def scrape(
    config_path: str = typer.Option("config/targets.yaml", "--config", "-c", help="Path to config YAML"),
):
    """Run the full agent-based scrape against configured categories."""
    load_dotenv()
    cfg = load_config(config_path)
    _setup_logging(cfg.logging.level, cfg.logging.path)

    logger.info(f"Loaded config: {len(cfg.categories)} categories | LLM={cfg.llm.provider}/{cfg.llm.model}")

    orchestrator = Orchestrator(cfg)
    summary = asyncio.run(orchestrator.run())
    typer.echo(f"\nSummary: {summary}")


@app.command()
def export(
    jsonl: str = typer.Option("data/output/products.jsonl", help="Input JSONL"),
    csv: str = typer.Option("data/output/products.csv", help="Output CSV"),
):
    """Re-export current JSONL to CSV (no scraping)."""
    rows = csv_export(jsonl, csv)
    typer.echo(f"Exported {rows} rows → {csv}")


@app.command()
def status(
    checkpoint: str = typer.Option("data/checkpoints/seen.json", help="Checkpoint file"),
    jsonl: str = typer.Option("data/output/products.jsonl", help="Output JSONL"),
):
    """Show how many URLs processed + how many products extracted."""
    from .storage import CheckpointStore, JsonlWriter
    cp = CheckpointStore(checkpoint)
    products = JsonlWriter(jsonl).read_all()
    typer.echo(f"Checkpoint: {len(cp)} URLs processed")
    typer.echo(f"Products: {len(products)} extracted")
    if products:
        by_method: dict[str, int] = {}
        for p in products:
            by_method[p.extraction_method.value] = by_method.get(p.extraction_method.value, 0) + 1
        typer.echo(f"By method: {by_method}")


if __name__ == "__main__":
    app()
