"""End-to-end orchestrator for the Multi-Collab metric pipeline:

    login -> navigate -> search -> [select approved rows -> open View ->
    extract metric grid -> back] per page -> reshape to wide -> stage raw ->
    validate schema -> upsert into master workbook -> (optional) reconcile
    against a manual baseline.

Run with: uv run python -m gfpvan_pipeline.main [--baseline path/to/manual_download.xlsx]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from .config import Config, ConfigError
from .extract import upsert_to_excel
from .landing import stage_raw
from .logger import get_logger
from .multi_collab_extract import run_download_extraction, run_multi_collab_extraction
from .reconciliation import reconcile, write_reconciliation_report
from .schema_validation import MULTI_COLLAB_SCHEMA, validate_extract
from .scraper import ScraperError, open_browser

log = get_logger(__name__)


class PipelineError(RuntimeError):
    pass


def run(cfg: Config, max_pages: int | None = None, baseline_path: Path | None = None) -> Path:
    """Run the full Multi-Collab pipeline once, one country at a time per
    config's search_scope.countries (each searched against the full
    search_scope.l5_products list). Returns the path to the updated master
    workbook. Raises PipelineError on a schema validation failure (config's
    validation.fail_on_error) - the raw landing capture is preserved either
    way, so nothing is lost even on a hard stop.

    extraction.method in config.yaml picks how each country's data is
    pulled off the pivot table page:
      "download" (default) - export_results_tsv()'s confirmed real flow:
        click the download icon, click Export, read the resulting .tsv.
        Verified to mechanically work; the real column names in an actual
        download have not yet been confirmed, so a validation failure
        reporting "missing columns" on a real run is expected and useful -
        it tells you the actual columns to reconcile against
        metric_to_column / MULTI_COLLAB_SCHEMA, not a sign this is broken.
      "scrape" - the older DOM-scraping path (extract_metric_grid /
        reshape_to_wide) against the pivot table's live HTML. Kept for
        reference/fallback; never confirmed against the real portal.
    """
    countries = cfg.get("search_scope.countries", [])
    products = cfg.get("search_scope.l5_products", [])
    if not countries:
        raise PipelineError("config.yaml's search_scope.countries is empty - nothing to search")
    if not products:
        raise PipelineError("config.yaml's search_scope.l5_products is empty - nothing to search")

    extraction_method = cfg.get("extraction.method", "download")

    per_country_frames: list[pd.DataFrame] = []
    with open_browser(cfg) as s:
        s.ensure_logged_in()
        s.open_gfpvan()
        s.open_search_supply_planning()

        for country in countries:
            log.info("=== Country %d/%d: %s ===", countries.index(country) + 1, len(countries), country)
            try:
                s.run_search(country, products)
            except ScraperError as e:
                log.error("Search failed for %s: %s - skipping this country", country, e)
                continue

            if extraction_method == "scrape":
                country_df = run_multi_collab_extraction(s, max_pages=max_pages)
            else:
                download_dir = cfg.get("output.download_dir", "./run_data/downloads")
                country_df = run_download_extraction(
                    s,
                    download_dir=download_dir,
                    max_pages=max_pages,
                    select_all=cfg.get("extraction.select_all", False),
                )
            if country_df.empty:
                log.warning("No rows extracted for %s", country)
            else:
                per_country_frames.append(country_df)

    wide_df = (
        pd.concat(per_country_frames, ignore_index=True)
        if per_country_frames
        else pd.DataFrame()
    )

    landing_path = stage_raw(
        wide_df.to_dict("records") if not wide_df.empty else [], cfg
    )
    log.info("Staged raw Multi-Collab extract (%d countries) to %s", len(per_country_frames), landing_path)

    validation = validate_extract(wide_df, schema=MULTI_COLLAB_SCHEMA)
    if not validation.ok:
        log.warning(validation.summary())
        if cfg.get("validation.fail_on_error", True):
            raise PipelineError(
                f"{validation.summary()} Raw payload preserved at "
                f"{landing_path} for diagnosis - re-run against it once fixed."
            )

    master_path = upsert_to_excel(wide_df, cfg)
    log.info("Pipeline complete: master workbook at %s", master_path)

    if baseline_path is not None:
        from .reconciliation import load_baseline

        baseline_df = load_baseline(baseline_path)
        report = reconcile(
            wide_df,
            baseline_df,
            kpi_columns=cfg.get("reconciliation.kpi_columns", []),
            group_by=cfg.get("reconciliation.group_by"),
            tolerance_pct=cfg.get("reconciliation.tolerance_pct", 1.0),
        )
        report_path = Path(cfg.get("output.dir", "./run_data/extracts")) / "reconciliation_report.xlsx"
        write_reconciliation_report(report, report_path)
        if not report.ok:
            log.warning("Reconciliation did not pass - see %s", report_path)

    return master_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the GFPVAN Multi-Collab pipeline")
    parser.add_argument("--config", default="config.yaml", type=Path)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Path to a manually-downloaded baseline (.xlsx/.csv) to reconcile against",
    )
    args = parser.parse_args()

    try:
        cfg = Config.load(args.config)
    except ConfigError as e:
        log.error("Config error: %s", e)
        return 1

    try:
        master_path = run(cfg, max_pages=args.max_pages, baseline_path=args.baseline)
    except (PipelineError, ScraperError) as e:
        log.error("Pipeline failed: %s", e)
        return 1

    print(f"Master workbook: {master_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
