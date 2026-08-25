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
