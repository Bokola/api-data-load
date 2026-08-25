"""Landing zone: persist the raw scraped payload (a list of dict records,
straight off the grid, before any schema validation or reshaping) to disk.

Why this exists as its own step: if schema validation or reconciliation
later finds a problem, you can re-run the transform against this file
without re-scraping GFPVAN - and if the portal changes its DOM in a way
that silently corrupts a downstream step, you still have exactly what the
page returned to diagnose it. Nothing here is deleted or overwritten; every
run gets its own timestamped file.

Requires: pip install pyarrow  (or: uv add pyarrow) - only needed for the
default parquet format; the json format has no extra dependency.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from .config import Config
from .logger import get_logger

log = get_logger(__name__)


def stage_raw(records: list[dict], cfg: Config, run_id: str | None = None) -> Path:
    """Write raw records to the landing zone. Returns the path written.

    Format is controlled by landing.format in config.yaml:
      - "parquet" (default): compact, typed-enough for downstream pandas/
        Fabric reads. Values are cast to string first, since everything off
        a scraped grid arrives as text regardless of its real type - typing
        is a job for the schema-validation step, not the raw capture.
      - "json": human-readable, easiest to eyeball or diff by hand.
    """
    landing_dir = Path(cfg.get("landing.dir", "./run_data/landing"))
    landing_dir.mkdir(parents=True, exist_ok=True)

    run_id = run_id or datetime.now().strftime("%Y%m%dT%H%M%S")
    fmt = cfg.get("landing.format", "parquet").lower()

    if fmt == "parquet":
        path = landing_dir / f"gfpvan_raw_{run_id}.parquet"
        df = pd.DataFrame(records).astype(str) if records else pd.DataFrame()
        df.to_parquet(path, index=False)
    elif fmt == "json":
        path = landing_dir / f"gfpvan_raw_{run_id}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2, default=str)
    else:
        raise ValueError(f"Unknown landing.format: {fmt!r} - use 'parquet' or 'json'")

    log.info("Staged %d raw records to %s", len(records), path)
    return path


def load_raw(path: str | Path) -> list[dict]:
    """Read a previously staged landing file back into a list of dicts -
    for replaying the transform step without re-scraping."""
    path = Path(path)
    if path.suffix == ".parquet":
        return pd.read_parquet(path).to_dict("records")
    if path.suffix == ".json":
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    raise ValueError(f"Unrecognized landing file extension: {path.suffix}")
