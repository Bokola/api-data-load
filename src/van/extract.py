"""Data extraction: pull the GFPVAN results grid into a pandas DataFrame and
write it to an .xlsx file ready for upload into a Fabric lakehouse.

Column names are read dynamically from the grid's own header row rather than
hardcoded, so this keeps working if GFPVAN adds/renames/reorders columns.

Full pipeline order (see run_full_extraction): collect raw records -> stage
them to the landing zone untouched -> validate structure -> write the
structured .xlsx. Staging happens BEFORE validation on purpose - even a
run that fails validation leaves a raw capture on disk to diagnose against.

Typical use, chained onto the scraper flow in scraper.py:

    with open_browser(cfg) as s:
        s.ensure_logged_in()
        s.open_gfpvan()
        s.open_search_supply_planning()
        s.run_search_approved_only()

        df, snapshot_path, master_path, landing_path, validation = run_full_extraction(
            s, cfg, write_master=True
        )
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from .config import Config
from .landing import stage_raw
from .logger import get_logger
from .schema_validation import ValidationResult, validate_extract
from .scraper import GFPVANScraper

log = get_logger(__name__)


class ExtractionValidationError(RuntimeError):
    """Raised when schema validation fails and validation.fail_on_error is
    true. The landing-zone raw capture is preserved either way - only the
    structured .xlsx write is skipped."""


def _extract_current_page(scraper: GFPVANScraper) -> list[dict]:
    """Read every row on the currently-loaded results page into a list of
    dicts, keyed by the grid's own column headers."""
    assert scraper.page is not None

    header_cells = scraper.page.locator("thead th, [role=columnheader]")
    n_cols = header_cells.count()
    if n_cols == 0:
        log.warning("No header cells found on current page - skipping")
        return []

    headers = []
    for i in range(n_cols):
        text = header_cells.nth(i).inner_text().strip()
        # Guard against duplicate/blank header text (e.g. a leading checkbox
        # column with no label) producing collided dict keys.
        headers.append(text if text else f"column_{i}")
    seen: dict[str, int] = {}
    deduped_headers = []
    for h in headers:
        if h in seen:
            seen[h] += 1
            deduped_headers.append(f"{h}_{seen[h]}")
        else:
            seen[h] = 0
            deduped_headers.append(h)

    row_selector = None
    for sel in scraper.cfg.selectors("results.row"):
        if scraper.page.locator(sel).count() > 0:
            row_selector = sel
            break
    if row_selector is None:
        log.warning("No result rows found on current page")
        return []

    rows = scraper.page.locator(row_selector)
    records = []
    for r in range(rows.count()):
        row = rows.nth(r)
        cells = row.locator("td, [role=gridcell]")
        cell_count = cells.count()
        record = {}
        for i, header in enumerate(deduped_headers):
            if i >= cell_count:
                record[header] = None
                continue
            record[header] = cells.nth(i).inner_text().strip()
        records.append(record)

    log.info("Extracted %d rows from current page", len(records))
    return records


def _collect_all_pages_records(
    scraper: GFPVANScraper, max_pages: int | None = None
) -> list[dict]:
    """Walk every page of the current search results and return the raw
    list of dict records - the thing that gets staged to the landing zone
    before any DataFrame/schema work touches it."""
    assert scraper.page is not None

    total = scraper.total_pages()
    if max_pages is not None:
        total = min(total, max_pages)
    log.info("Extracting %d page(s) of results", total)

    all_records: list[dict] = []
    extracted_at = datetime.now().isoformat(timespec="seconds")

    page_num = 1
    while True:
        page_records = _extract_current_page(scraper)
        for rec in page_records:
            rec["_source_page"] = page_num
            rec["_extracted_at"] = extracted_at
        all_records.extend(page_records)

        if page_num >= total:
            break
        if not scraper.goto_next_page():
            log.warning(
                "Expected %d pages but pagination stopped after page %d",
                total, page_num,
            )
            break
        page_num += 1

    if not all_records:
        log.warning("No records extracted across %d page(s)", page_num)

    return all_records


def extract_all_pages(scraper: GFPVANScraper, max_pages: int | None = None) -> pd.DataFrame:
    """Convenience wrapper around _collect_all_pages_records() for callers
    that just want a DataFrame with no landing/validation stage - most
    callers should use run_full_extraction() instead."""
    records = _collect_all_pages_records(scraper, max_pages=max_pages)
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    log.info("Extraction complete: %d rows, %d columns", len(df), len(df.columns))
    return df


def _reorder_columns(df: pd.DataFrame, excel_columns: list[str]) -> pd.DataFrame:
    """Put columns in the order given by config's excel_columns, dropping
    ones that aren't present and appending anything extra at the end (so a
    new metric/field never silently disappears - it just lands last until
    you add it to excel_columns)."""
    if not excel_columns or df.empty:
        return df
    ordered = [c for c in excel_columns if c in df.columns]
    extra = [c for c in df.columns if c not in excel_columns]
    return df[ordered + extra]


def _write_formatted_excel(df: pd.DataFrame, path: Path, sheet_name: str) -> None:
    """Shared formatting: bold+frozen header row, auto-sized columns."""
    if df.empty:
        log.warning("DataFrame is empty - writing a header-only workbook to %s", path)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name=sheet_name, index=False)
        worksheet = writer.sheets[sheet_name]

        for cell in worksheet[1]:
            cell.font = cell.font.copy(bold=True)
        worksheet.freeze_panes = "A2"

        for col_idx, column in enumerate(df.columns, start=1):
            max_len = max(
                [len(str(column))] + [len(str(v)) for v in df[column].astype(str)]
            ) if len(df) else len(str(column))
            col_letter = worksheet.cell(row=1, column=col_idx).column_letter
            worksheet.column_dimensions[col_letter].width = min(max_len + 2, 60)


def write_to_excel(
    df: pd.DataFrame,
    cfg: Config,
    filename: str | None = None,
) -> Path:
    """Write the extracted DataFrame to a fresh, timestamped .xlsx snapshot
    under the configured output directory. Returns the path written.

    This is the audit-trail write - every run gets its own file, nothing is
    ever overwritten. For a single persistent workbook that gets updated in
    place (matching config's dedup_key), use upsert_to_excel() instead.
    """
    out_dir = Path(cfg.get("output.dir", "./run_data/extracts"))
    out_dir.mkdir(parents=True, exist_ok=True)

    if filename is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"gfpvan_extract_{ts}.xlsx"
    path = out_dir / filename

    sheet_name = cfg.get("output.sheet_name", "GFPVAN Extract")
    df = _reorder_columns(df, cfg.get("excel_columns", []))
    _write_formatted_excel(df, path, sheet_name)

    log.info("Wrote %d rows to snapshot %s", len(df), path)
    return path


def upsert_to_excel(df_new: pd.DataFrame, cfg: Config) -> Path:
    """Write df_new into a persistent master workbook, updating existing
    rows in place rather than duplicating them - matched on config's
    dedup_key (e.g. Product + Country + Period).

    If the master workbook doesn't exist yet, it's created fresh. If
    dedup_key columns aren't all present in both the existing workbook and
    df_new, falls back to a plain append (with a warning) rather than
    silently dropping rows.
    """
    out_dir = Path(cfg.get("output.dir", "./run_data/extracts"))
    out_dir.mkdir(parents=True, exist_ok=True)
    master_path = out_dir / cfg.get("output.master_filename", "gfpvan_master.xlsx")
    sheet_name = cfg.get("output.sheet_name", "GFPVAN Extract")
    excel_columns = cfg.get("excel_columns", [])
    dedup_key = cfg.get("dedup_key", [])

    df_new = _reorder_columns(df_new, excel_columns)

    if master_path.exists():
        existing = pd.read_excel(master_path, sheet_name=sheet_name)
        existing = _reorder_columns(existing, excel_columns)

        key_cols_present = (
            dedup_key
            and all(k in existing.columns for k in dedup_key)
            and all(k in df_new.columns for k in dedup_key)
        )
        if key_cols_present:
            combined = pd.concat([existing, df_new], ignore_index=True)
            before = len(combined)
            # keep="last" -> the new extraction's values win on a key clash
            combined = combined.drop_duplicates(subset=dedup_key, keep="last")
            log.info(
                "Upsert: %d existing + %d new rows -> %d after dedup on %s "
                "(%d row(s) updated in place)",
                len(existing), len(df_new), len(combined),
                dedup_key, before - len(combined),
            )
        else:
            log.warning(
                "dedup_key %s not fully present in both existing and new "
                "data - appending without dedup instead of upserting",
                dedup_key,
            )
            combined = pd.concat([existing, df_new], ignore_index=True)
    else:
        combined = df_new
        log.info("No existing master workbook at %s - creating it fresh", master_path)

    combined = _reorder_columns(combined, excel_columns)
    _write_formatted_excel(combined, master_path, sheet_name)
    return master_path


def run_full_extraction(
    scraper: GFPVANScraper,
    cfg: Config,
    max_pages: int | None = None,
    filename: str | None = None,
    fail_on_validation_error: bool | None = None,
    write_master: bool = False,
) -> tuple[pd.DataFrame, Path | None, Path | None, Path, ValidationResult]:
    """Full pipeline: collect raw records -> stage to landing zone -> run
    structural validation -> write the structured .xlsx.

    Returns (dataframe, snapshot_xlsx_path_or_None, master_xlsx_path_or_None,
    landing_path, validation_result). Both xlsx paths are None if validation
    failed and fail_on_validation_error was true - the landing file is still
    written in that case, so nothing is lost, it just isn't promoted to a
    structured output.

    write_master=True also upserts into the persistent master workbook
    (config's output.master_filename), matched on dedup_key, in addition to
    the timestamped snapshot. Use this for the Search Results grid extract;
    for the Multi-Collab metric extract use multi_collab_extract.py's
    reshape_to_wide() output with upsert_to_excel() directly, since that's
    the shape dedup_key/excel_columns actually describe.

    fail_on_validation_error defaults to config's validation.fail_on_error
    (True unless set otherwise). Pass it explicitly to override per-call.
    """
    records = _collect_all_pages_records(scraper, max_pages=max_pages)

    landing_path = stage_raw(records, cfg)

    df = pd.DataFrame(records) if records else pd.DataFrame()
    validation = validate_extract(df)

    if not validation.ok:
        log.warning(validation.summary())

    fail_on_error = (
        cfg.get("validation.fail_on_error", True)
        if fail_on_validation_error is None
        else fail_on_validation_error
    )

    if not validation.ok and fail_on_error:
        raise ExtractionValidationError(
            f"{validation.summary()} Raw payload preserved at {landing_path} "
            "for diagnosis - re-run the transform against it once fixed."
        )

    snapshot_path = write_to_excel(df, cfg, filename=filename)
    master_path = upsert_to_excel(df, cfg) if write_master else None
    return df, snapshot_path, master_path, landing_path, validation
