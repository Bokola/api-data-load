# GFPVAN pipeline

Scrape GFPVAN -> stage raw payload -> validate schema -> extract Multi-Collab
metrics -> upsert into a master Excel workbook -> (optional) reconcile
against a manual baseline.

## Why the layout matters

Every module (`scraper.py`, `extract.py`, etc.) uses relative imports like
`from .config import Config`. That only works when the file is executed as
part of the installed `gfpvan_pipeline` package - e.g.
`python -m gfpvan_pipeline.main`. Running a file directly
(`python src/gfpvan_pipeline/scraper.py`) will always fail with
`ImportError: attempted relative import with no known parent package`,
because Python has no package context to resolve `.config` against. Always
invoke through `-m`, the console script, or another module's own import -
never as a standalone script.

## Setup

```bash
# 1. Create the venv and install everything from pyproject.toml
uv venv
uv sync

# 2. Install the actual browser binary Playwright drives
uv run playwright install chromium
# If your OS is missing shared libs Chromium needs, use --with-deps instead
# (needs sudo/apt access): uv run playwright install chromium --with-deps

# 3. Set credentials (never commit real values)
cp .env.example .env
# edit .env with your real GFPVAN_USERNAME / GFPVAN_PASSWORD
```

## Running

`Config.load()` loads `.env` itself via `python-dotenv` (searching upward from
the current directory), so plain `uv run python -m gfpvan_pipeline.main` picks
up your credentials automatically - you don't need `--env-file .env`. Real
shell/CI-exported env vars always take priority over `.env` if both are set.

```bash
# Full pipeline - .env is picked up automatically
uv run python -m gfpvan_pipeline.main

# Cap how many result pages to process per country (useful while testing selectors)
uv run python -m gfpvan_pipeline.main --max-pages 1

# Reconcile against a manually-downloaded baseline file
uv run python -m gfpvan_pipeline.main --baseline path/to/manual_download.xlsx

# Equivalent, via the installed console script
uv run gfpvan-pipeline --max-pages 1
```

Watch it run instead of headless: set `headless: false` in `config.yaml`.

## Project layout

```
config.yaml                          # everything hot-swappable without code edits
.env.example                         # copy to .env, fill in real credentials
pyproject.toml                       # dependencies + console script entry point
src/gfpvan_pipeline/
  config.py           Config.load(path) - config.yaml + env-var credentials
  logger.py           shared logging setup
  scraper.py           Playwright driver: login, navigate, search, paginate
  extract.py            Search Results grid extraction + Excel writer/upsert
  multi_collab_extract.py  Multi-Collab metric grid extraction + reshape
  landing.py             raw payload staging (parquet/json)
  schema_validation.py   pandera structural checks
  reconciliation.py       automated vs. manual-baseline comparison
  main.py                 end-to-end orchestrator + CLI
run_data/               gitignored - screenshots, landing zone, extracts, session state
```

## Call graph

```
main.py
 ├─ config.py            Config.load()
 ├─ logger.py             enable_file_logging() → one timestamped run_data/logs/pipeline_*.log per run
 ├─ scraper.py            open_browser() → GFPVANScraper:
 │                          ensure_logged_in() → login() → _login_once()
 │                          open_gfpvan(), open_search_supply_planning()   (re-run before EVERY country)
 │                          run_search() → _select_autocomplete_option(), _clear_autocomplete_selections()
 │                          _wait_networkidle()   (every networkidle wait goes through this - see below)
 ├─ multi_collab_extract.py
 │    ├─ extraction.method: "download" (default) - the confirmed real export flow:
 │    │     run_download_extraction(scraper, download_dir, country, ...)
 │    │       └─ calls scraper's select_approved_rows() / select_all_rows() → open_view() →
 │    │                 export_results_tsv() → back_to_results() → goto_next_page(), in a loop
 │    │       └─ parse_mcv_tsv() and parse_mcv_tsv_long() → _parse_mcv_long_records()  (shared core)
 │    │       returns (wide_df, long_df)
 │    └─ extraction.method: "scrape" (older DOM-scraping fallback, unconfirmed against the real portal):
 │          run_multi_collab_extraction(scraper, ...)
 │            └─ calls scraper's select_approved_rows() / open_view() / back_to_results() /
 │                      goto_next_page() in a loop
 │            └─ extract_metric_grid() → _extract_bucket_headers(), _extract_block_metrics()
 │            └─ reshape_to_wide()
 ├─ landing.py             stage_raw(records, cfg)
 ├─ schema_validation.py   validate_extract(wide_df, schema=MULTI_COLLAB_SCHEMA)
 ├─ extract.py             upsert_to_excel(wide_df, cfg) → _reorder_columns(), _write_formatted_excel()
 ├─ main.py                _write_country_csv(long_df, country, cfg)   (one CSV per country, long format)
 └─ reconciliation.py      load_baseline(), reconcile(), write_reconciliation_report()
                           (only if --baseline was passed)

extract.py  (independent, older Search-Results-grid path; not used by main.py's
             run(), but still imports scraper/landing/schema_validation the same way)
 ├─ scraper.py       (type hints only)
 ├─ landing.py        stage_raw()
 └─ schema_validation.py  validate_extract()
```

## Common issues

- **`ImportError: attempted relative import with no known parent package`**
  You ran a file directly instead of through `-m`. See "Why the layout
  matters" above.
- **`ConfigError: Missing required environment variable(s)`**
  `.env` isn't being loaded, or the var names in it don't match
  `credentials.username_env` / `password_env` in `config.yaml`. Run with
  `uv run --env-file .env ...`, not plain `uv run ...`.
- **Playwright browser download fails / blocked host**
  Some sandboxed environments block `cdn.playwright.dev`. Run
  `uv run playwright install chromium` from a machine with normal internet
  access, or point `PLAYWRIGHT_BROWSERS_PATH` at a pre-downloaded browser.
