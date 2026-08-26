"""Extract per-product/per-country metric time series from an open
Multi-Collab View, and reshape them into the wide schema implied by
config.yaml's metric_to_column / excel_columns / dedup_key.

ASSUMED DOM SHAPE (unverified against the live portal - adjust the
multi_collab.* selectors in config.yaml once confirmed):

    <container>                              selectors: multi_collab.container
      <collab_block>                         selectors: multi_collab.collab_block
                                              (one per Product + Country pair
                                              opened via open_view())
        <bucket_header_row>                  selectors: multi_collab.bucket_header_row
          <th>Metric</th><th>Jan 2026</th><th>Feb 2026</th>...
        <metric_row>                         selectors: multi_collab.metric_row
          <td>Monthly Consumption</td><td>1200</td><td>1300</td>...
          <td>In Process Requisitions</td><td>40</td><td>55</td>...
          ...

Product/Country are NOT assumed to be readable from inside the block itself
(unconfirmed whether the portal repeats them there) - instead they come from
the context list scraper.select_approved_rows() returns, matched positionally
to collab_blocks in the order they appear. If a run ever ticks more/fewer
rows than there are collab_blocks, extraction logs a warning and matches as
far as it can rather than raising - a partial extract you can see is better
than a hard crash you have to debug blind.

Flow, chained onto the scraper + select_approved_rows flow:

    with open_browser(cfg) as s:
        s.ensure_logged_in()
        s.open_gfpvan()
        s.open_search_supply_planning()
        s.run_search_approved_only()

        all_records = []
        while True:
            contexts = s.select_approved_rows()
            if contexts and s.open_view():
                all_records.extend(extract_metric_grid(s, contexts, cfg))
                s.back_to_results()
            if not s.goto_next_page():
                break

        wide_df = reshape_to_wide(all_records, cfg)
"""
from __future__ import annotations

import re

import pandas as pd

from .config import Config
from .logger import get_logger
from .scraper import GFPVANScraper

log = get_logger(__name__)


def _extract_bucket_headers(block, cfg: Config) -> list[str]:
    """Return the time-bucket labels (e.g. 'Jan 2026') from a collab block's
    header row, skipping the first column (which holds the metric name)."""
    for sel in cfg.selectors("multi_collab.bucket_header_row"):
        header_row = block.locator(sel).first
        if header_row.count() > 0:
            cells = header_row.locator("th, td")
            n = cells.count()
            if n <= 1:
                continue
            return [cells.nth(i).inner_text().strip() for i in range(1, n)]
    return []


def _extract_block_metrics(
    block, bucket_headers: list[str], context: dict, cfg: Config
) -> list[dict]:
    """Read every metric row in one collab block into long-format records:
    one row per (Product, Country, Period, metric_name, value)."""
    records: list[dict] = []
    for sel in cfg.selectors("multi_collab.metric_row"):
        rows = block.locator(sel)
        n = rows.count()
        if n == 0:
            continue
        for r in range(n):
            row = rows.nth(r)
            cells = row.locator("td, th")
            cell_count = cells.count()
            if cell_count == 0:
                continue
            metric_name = cells.nth(0).inner_text().strip()
            if not metric_name:
                continue
            for i, bucket in enumerate(bucket_headers, start=1):
                if i >= cell_count:
                    break
                value = cells.nth(i).inner_text().strip()
                records.append(
                    {
                        "Product": context.get("product"),
                        "Country": context.get("country"),
                        "Period": bucket,
                        "metric_name": metric_name,
                        "value": value,
                    }
                )
        break  # first selector that yields any rows wins, like first_match
    return records


def extract_metric_grid(
    scraper: GFPVANScraper, contexts: list[dict]
) -> list[dict]:
    """Extract every collab block on the currently-open Multi-Collab View.

    contexts is the list returned by scraper.select_approved_rows() -
    matched positionally to collab blocks in DOM order. Returns long-format
    records ready for reshape_to_wide().
    """
    assert scraper.page is not None
    cfg = scraper.cfg

    block_selector = None
    for sel in cfg.selectors("multi_collab.collab_block"):
        if scraper.page.locator(sel).count() > 0:
            block_selector = sel
            break
    if block_selector is None:
        log.warning("Could not locate any collab blocks in the Multi-Collab View")
        return []

    blocks = scraper.page.locator(block_selector)
    n_blocks = blocks.count()
    if n_blocks != len(contexts):
        log.warning(
            "Ticked %d row(s) but found %d collab block(s) - matching "
            "positionally as far as possible; extra blocks get no "
            "Product/Country context, extra contexts are unused.",
            len(contexts), n_blocks,
        )

    all_records: list[dict] = []
    for i in range(n_blocks):
        block = blocks.nth(i)
        context = contexts[i] if i < len(contexts) else {"product": None, "country": None}

        bucket_headers = _extract_bucket_headers(block, cfg)
        if not bucket_headers:
            log.warning(
                "Collab block %d (product=%s, country=%s): no bucket header "
                "row found - skipping", i, context.get("product"), context.get("country"),
            )
            continue

        block_records = _extract_block_metrics(block, bucket_headers, context, cfg)
        log.info(
            "Collab block %d (product=%s, country=%s): extracted %d metric "
            "values across %d bucket(s)",
            i, context.get("product"), context.get("country"),
            len(block_records), len(bucket_headers),
        )
        all_records.extend(block_records)

    return all_records


def reshape_to_wide(records: list[dict], cfg: Config) -> pd.DataFrame:
    """Pivot long-format (Product, Country, Period, metric_name, value)
    records into one row per (Product, Country, Period), metric names as
    columns via config's metric_to_column mapping. Unmapped metric names are
    kept, prefixed with 'raw_', so a metric the portal adds later doesn't
    silently vanish - it just shows up unmapped until you add it to the
    config.
    """
    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    mapping: dict = cfg.get("metric_to_column", {})

    df["_column"] = df["metric_name"].map(lambda m: mapping.get(m, f"raw_{m}"))

    wide = df.pivot_table(
        index=["Product", "Country", "Period"],
        columns="_column",
        values="value",
        aggfunc="first",
    ).reset_index()
    wide.columns.name = None

    log.info(
        "Reshaped %d long-format values into %d wide rows, %d columns",
        len(df), len(wide), len(wide.columns),
    )
    return wide


def run_multi_collab_extraction(
    scraper: GFPVANScraper, max_pages: int | None = None
) -> pd.DataFrame:
    """Full per-page loop: select approved rows, open the Multi-Collab View,
    extract every collab block, go back, repeat across pages. Returns the
    wide-format DataFrame ready for landing/validation/write.
    """
    assert scraper.page is not None
    cfg = scraper.cfg

    total = scraper.total_pages()
    if max_pages is not None:
        total = min(total, max_pages)

    all_records: list[dict] = []
    page_num = 1
    while True:
        try:
            contexts = scraper.select_approved_rows()
            if contexts:
                if scraper.open_view():
                    all_records.extend(extract_metric_grid(scraper, contexts))
                    scraper.back_to_results()
                else:
                    log.warning(
                        "Page %d: %d approved row(s) ticked but View failed to "
                        "open - skipping this page's extraction",
                        page_num, len(contexts),
                    )
            else:
                log.info("Page %d: no approved rows to extract", page_num)
        except Exception:
            # Nothing in this per-page block used to dump diagnostics on
            # failure - only run_search() did. Once search itself started
            # succeeding, an uncaught exception here (this whole post-search
            # flow - Collaboration Selector grid, View, pivot table - was
            # unverified against the real portal until now) propagated all
            # the way up with zero forensic capture. Dump here too, then
            # re-raise unchanged so callers' error handling is unaffected.
            scraper.dump_diagnostics(f"multi_collab_extraction_failed_page{page_num}")
            raise

        if page_num >= total:
            break
        if not scraper.goto_next_page():
            log.warning(
                "Expected %d pages but pagination stopped after page %d",
                total, page_num,
            )
            break
        page_num += 1

    return reshape_to_wide(all_records, cfg)


_MONTH_COLUMN_PATTERN = re.compile(r"^\d{4}-\d{2}$")
_UNNAMED_COLUMN_PATTERN = re.compile(r"^(Unnamed: \d+|__unnamed_\d+__)$")


def _parse_mcv_long_records(tsv_path, country: str) -> list[dict]:
    """Shared parsing core for a downloaded Multi-Collab View .tsv. Reads
    the file and returns long-format records - one per (product, bucket,
    metric, month) reading - with keys 'Country Name', 'Supply Plan Bucket
    Description', 'L5 - Product', 'DataMeasure', 'Date', 'Value'.

    Both parse_mcv_tsv() (wide, pivots metrics into columns, for the
    master workbook/schema validation) and parse_mcv_tsv_long() (for CSV
    export, matching a manually-downloaded reference export exactly)
    build on this, so the actual file-reading and column-identification
    logic - the genuinely tricky part, confirmed against a real file, see
    the docstrings below for what was wrong before - exists in exactly
    one place.

    Confirmed real structure from an actual downloaded file (not guessed):
      - File line 1 is title/metadata ('Collaboration View', a bucket
        date-range note) - not real headers. pandas' header=1 skips it and
        uses line 2 (the real header row) instead.
      - The real header row names the static per-record attributes
        (Country ISO Code, Supply Plan Bucket Description, L5 - Product,
        Review Status - Inventory, ...) and, later, one column per monthly
        bucket (2026-02, 2026-03, ...). TWO columns in between are blank
        even in this header line - one of them holds the metric name on
        metric rows (see below); confirmed a real file can have more than
        one blank-named column, which breaks any column lookup by name
        unless they're first made unique.
      - Each product/country "block" spans multiple rows: one MASTER row
        (static attributes populated, metric-name and bucket-value columns
        blank) followed by several METRIC rows (static columns blank, the
        unlabeled metric-name column holds a name like 'Monthly
        Consumption', and the monthly columns hold that metric's value per
        period).

    `country` is the country actually searched (e.g. 'Kenya'), not the
    ISO code (e.g. 'KE') this export's own 'Country ISO Code' column
    holds - confirmed a manually-downloaded reference export uses the
    full name for this, so it's taken as a parameter here rather than
    guessing an ISO-to-name mapping table.

    DataMeasure values are the RAW metric name exactly as the export
    provides it (e.g. 'Projected Inventory Adjustment') - NOT run through
    metric_to_column. That mapping exists specifically for the wide
    shape's column names and would rename things a manual reference
    export leaves alone.
    """
    raw = pd.read_csv(tsv_path, sep="\t", header=1)

    # Make every blank/NaN column name unique by position BEFORE any
    # column-based indexing - defensive: confirmed a real file's blank
    # header cells actually come back from pandas as "Unnamed: N" strings
    # (not NaN), which are already unique by position, but this guards
    # against a literal NaN column name from some other read path too.
    raw.columns = [
        c if pd.notna(c) else f"__unnamed_{i}__"
        for i, c in enumerate(raw.columns)
    ]

    month_cols = [c for c in raw.columns if isinstance(c, str) and _MONTH_COLUMN_PATTERN.fullmatch(c)]
    if not month_cols:
        raise ValueError(f"Could not find any monthly bucket columns (YYYY-MM) in {tsv_path}")
    first_month_pos = raw.columns.get_loc(month_cols[0])

    # Find the metric-name column by identity, not a fixed positional
    # offset. An earlier version assumed it always sits exactly 2
    # positions before the first month column - true for one real file
    # (which happens to have exactly 2 blank-named columns there), but
    # WRONG in general: a file with a different number of blank columns
    # would silently pick the wrong column (confirmed: a simpler test
    # structure with only 1 blank column made this offset land on
    # "L5 - Product" itself, corrupting every metric name). Instead, scan
    # every unnamed column before the first month column (matching
    # pandas' own "Unnamed: N" auto-naming for blank header cells, not
    # just a literal NaN) and pick whichever one actually HAS data - a
    # genuine spacer column would be entirely empty, unlike the real
    # metric-name column.
    unnamed_before_months = [
        c for c in raw.columns[:first_month_pos] if _UNNAMED_COLUMN_PATTERN.match(str(c))
    ]
    if not unnamed_before_months:
        raise ValueError(
            f"Could not find an unnamed metric-name column before the first "
            f"month column in {tsv_path}"
        )
    # Pick whichever unnamed column has the MOST populated values, not just
    # any - confirmed a real file can have more than one unnamed column
    # with SOME data (e.g. 170 populated rows vs. 10), where the smaller
    # one is some other secondary field, not the metric name. The genuine
    # metric-name column is populated on nearly every metric row.
    metric_col = max(unnamed_before_months, key=lambda c: raw[c].notna().sum())

    # Static attributes are only populated on each block's master row -
    # forward-fill them down through that block's metric rows.
    for col in ("Country ISO Code", "Supply Plan Bucket Description", "L5 - Product"):
        if col in raw.columns:
            raw[col] = raw[col].ffill()

    metric_rows = raw[raw[metric_col].notna()]
    if metric_rows.empty:
        log.warning("No metric rows found in %s after parsing", tsv_path)
        return []

    def _clean_value(val):
        if isinstance(val, str):
            val = val.replace(",", "").strip()
        try:
            return float(val)
        except (TypeError, ValueError):
            return val

    long_records: list[dict] = []
    for _, row in metric_rows.iterrows():
        metric_name = row[metric_col]
        for month_col in month_cols:
            val = row[month_col]
            # Skip a genuinely missing reading rather than let it through
            # as (or become) 0 - per an explicit request. pd.isna(val)
            # catches NaN, which is what pandas' own CSV/TSV parser
            # normally produces for a blank cell; the extra check for a
            # literal empty/whitespace-only string is defensive, in case
            # a blank cell ever survives as "" instead (not observed in a
            # real file so far, but cheap to guard against).
            if pd.isna(val) or (isinstance(val, str) and val.strip() == ""):
                continue
            long_records.append(
                {
                    "Country Name": country,
                    "Supply Plan Bucket Description": row.get("Supply Plan Bucket Description"),
                    "L5 - Product": row.get("L5 - Product"),
                    "DataMeasure": metric_name,
                    "Date": pd.Period(month_col, freq="M").to_timestamp(),
                    "Value": _clean_value(val),
                }
            )
    return long_records


def parse_mcv_tsv(tsv_path, metric_to_column: dict, country: str) -> pd.DataFrame:
    """Parse a downloaded Multi-Collab View .tsv into a wide DataFrame with
    Product/Country/Period columns and one column per metric (via
    metric_to_column) - the shape the master workbook/schema validation
    expect. See _parse_mcv_long_records() for the real file structure this
    is built from. Country is the full name passed in via `country`
    (confirmed to match a manual reference export), not the ISO code the
    file's own 'Country ISO Code' column holds.
    """
    long_records = _parse_mcv_long_records(tsv_path, country)
    if not long_records:
        return pd.DataFrame()

    long_df = pd.DataFrame(long_records)
    long_df["_column"] = long_df["DataMeasure"].map(
        lambda m: metric_to_column.get(m, f"raw_{m}")
    )
    long_df["Period"] = long_df["Date"].dt.strftime("%Y-%m")
    wide = long_df.pivot_table(
        index=["L5 - Product", "Country Name", "Period"],
        columns="_column",
        values="Value",
        aggfunc="first",
    ).reset_index()
    wide.columns.name = None
    return wide.rename(columns={"L5 - Product": "Product", "Country Name": "Country"})


def parse_mcv_tsv_long(tsv_path, country: str) -> pd.DataFrame:
    """Parse a downloaded Multi-Collab View .tsv into LONG format, matching
    a manually-downloaded reference export exactly: one row per (Country
    Name, Supply Plan Bucket Description, L5 - Product, DataMeasure,
    Period, Value) rather than pivoted into metric columns.

    Period is 'YYYY-MM' (not a full date) - matching the source .tsv's own
    month-bucket column labels directly, per an explicit request, rather
    than the first-of-month date _parse_mcv_long_records() builds
    internally for its own bookkeeping.

    Value is never filled in for a missing reading - per an explicit
    request, rows where the source cell was genuinely blank/NA are
    dropped, not defaulted to 0. This is already true structurally
    (_parse_mcv_long_records() skips a NaN cell via `if pd.isna(val):
    continue` before it's ever added to a record), so the .dropna() below
    is a no-op in practice - it's kept anyway, explicitly, at the exact
    point this DataFrame is finalized, so this contract is visible and
    auditable here rather than relying on a skip several calls away.

    NOTE: a row with Value == 0 is NOT dropped by this - 0 is a genuine
    reading the source export reported (confirmed: distinct from a blank
    cell, which never produces a row at all), not a stand-in for missing
    data. If you specifically want 0-valued readings excluded too, that's
    a different, additional filter - ask for it explicitly rather than
    assuming it's covered here, since 0 can be a legitimate data point
    (e.g. "zero units consumed this month").
    """
    long_records = _parse_mcv_long_records(tsv_path, country)
    if not long_records:
        return pd.DataFrame()

    df = pd.DataFrame(long_records)
    df["Period"] = df["Date"].dt.strftime("%Y-%m")
    df = df.dropna(subset=["Value"])
    return df[
        ["Country Name", "Supply Plan Bucket Description", "L5 - Product", "DataMeasure", "Period", "Value"]
    ]



def run_download_extraction(
    scraper: GFPVANScraper,
    download_dir: str,
    country: str,
    max_pages: int | None = None,
    select_all: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Full per-page loop using the confirmed real export flow instead of
    DOM-scraping the pivot table: select rows -> open View -> click the
    download icon -> Export -> parse the downloaded .tsv -> back to
    results -> repeat.

    select_all=False (default): use select_approved_rows() - only rows
    whose Review Status matches config's approved_statuses get ticked and
    exported, same filtering as the DOM-scrape path. select_all=True:
    use select_all_rows() instead - every row on the page gets ticked, no
    status filtering.

    Returns (wide_df, long_df), both built from the same downloaded .tsv
    per page without re-fetching anything:
      - wide_df: Product/Country/Period + one column per metric (via
        parse_mcv_tsv() and config's metric_to_column) - the shape the
        master workbook/schema validation expect.
      - long_df: Country Name/Supply Plan Bucket Description/L5 - Product/
        DataMeasure/Date/Value - one row per metric reading, matching a
        manually-downloaded reference export exactly (via
        parse_mcv_tsv_long()). This is what CSV export uses.
    `country` (the full name actually searched, e.g. 'Kenya') is used for
    both - confirmed a manual reference export uses the full name, not
    the ISO code the .tsv's own 'Country ISO Code' column holds.
    """
    assert scraper.page is not None
    metric_to_column = scraper.cfg.get("metric_to_column", {})

    total = scraper.total_pages()
    if max_pages is not None:
        total = min(total, max_pages)

    all_wide_frames: list[pd.DataFrame] = []
    all_long_frames: list[pd.DataFrame] = []
    page_num = 1
    logged_columns = False
    while True:
        try:
            contexts = (
                None if select_all else scraper.select_approved_rows()
            )
            ticked = (
                scraper.select_all_rows() if select_all else len(contexts or [])
            )
            if ticked:
                if scraper.open_view():
                    tsv_path = scraper.export_results_tsv(download_dir)
                    wide_page_df = parse_mcv_tsv(tsv_path, metric_to_column, country)
                    long_page_df = parse_mcv_tsv_long(tsv_path, country)
                    if not logged_columns:
                        log.info(
                            "Parsed export columns: %s",
                            list(wide_page_df.columns),
                        )
                        logged_columns = True
                    log.info(
                        "Page %d: downloaded %d row(s) from %s",
                        page_num, len(wide_page_df), tsv_path,
                    )
                    all_wide_frames.append(wide_page_df)
                    all_long_frames.append(long_page_df)
                    scraper.back_to_results()
                else:
                    log.warning(
                        "Page %d: %d row(s) ticked but View failed to open - "
                        "skipping this page's export",
                        page_num, ticked,
                    )
            else:
                log.info("Page %d: no rows to export", page_num)
        except Exception:
            # Same reasoning as run_multi_collab_extraction: this whole
            # post-search flow was unverified against the real portal until
            # recently, so capture evidence before letting a failure
            # propagate rather than losing it.
            scraper.dump_diagnostics(f"download_extraction_failed_page{page_num}")
            raise

        if page_num >= total:
            break
        if not scraper.goto_next_page():
            log.warning(
                "Expected %d pages but pagination stopped after page %d",
                total, page_num,
            )
            break
        page_num += 1

    wide_df = pd.concat(all_wide_frames, ignore_index=True) if all_wide_frames else pd.DataFrame()
    long_df = pd.concat(all_long_frames, ignore_index=True) if all_long_frames else pd.DataFrame()
    return wide_df, long_df
