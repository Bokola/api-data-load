# SKILL.md

# GFPVAN Pipeline Skill Documentation

## Overview
Automated browser scraping, schema validation, raw staging, and Excel master workbook upserting for GFPVAN (Global Family Planning VAN) supply planning data hosted on the e2open platform.

## Module Map & Responsibilities
* **`config.py`**: Configuration loader (`config.yaml`). Resolves environment variables (`GFPVAN_USERNAME`, `GFPVAN_PASSWORD`) via `dotenv` with fallback overrides. Implements dotted key lookups (`cfg.get()`) and selector auto-prefixing (`cfg.selectors()`).
* **`scraper.py`**: Playwright automation engine (`GFPVANScraper`).
  * Session state persistence (`storage_state.json`) with automatic login bypass.
  * Multi-frame locator resolution (`first_match()`) with frame caching.
  * Stealth initialization (`playwright-stealth`) and custom desktop UA.
  * Custom typeahead interactions (`_select_autocomplete_option`) for `eto-complex-autocomplete` UI components.
  * Error recovery, modal handling (`_dismiss_blocking_modal`), and multi-frame diagnostic HTML/screenshot dumping.
* **`extract.py`**: Grid table extraction (`pandas`). Collects raw paginated records, formats openpyxl Excel workbooks, and performs master workbook upserts on key collisions (`dedup_key`).
* **`multi_collab_extract.py`**: Specialized time-series metric extraction for Multi-Collab view. Unpivots long metric series into wide structures mapped via `metric_to_column`.
* **`landing.py`**: Raw payload staging layer (`stage_raw`, `load_raw`). Persists unvalidated scraped data directly to Parquet or JSON prior to schema checks.
* **`schema_validation.py`**: Pandera validation layer (`validate_extract`). Enforces structural schema presence, blank string checks, and required key integrity for both grid and multi-collab extracts.
* **`reconciliation.py`**: Audit layer (`reconcile`). Validates pipeline output against manual baseline downloads (.xlsx/.csv) by checking total row counts and aggregated KPI variances within tolerance limits.
* **`main.py`**: End-to-end orchestration CLI entry point. Loops across configured countries/products, coordinates scraping, staging, validation, master upserting, and reconciliation.
* **`debug_frames.py`**: **Planned, not yet implemented.** Would load a saved `dump_diagnostics()` capture (the `run_data/screenshots/*_frame*.html` files) and report frame count, which frame holds the search form, and a quick content summary of each — automating what's currently a manual `grep`/`view` pass over those files.

## Target Platform & Search Scope
* **Target Authentication**: SSO via e2open (`authn.e2open.com`) redirecting to Launchpad and GFPVAN Home.
* **Search Execution**: Runs sequentially by country and product scope (e.g., L5 injectable/subcutaneous products for Kenya).
* **Autocomplete Fields**: Target containers use `#CustomerDescription__Autocomplete` and `#CustItemDescription__Autocomplete` widgets.

## Confirmed Portal Layout

**This is a living reference, not a one-time note.** Every entry below was confirmed directly from a `dump_diagnostics()` capture during real debugging — none of it is guessed. When a new capture reveals something that contradicts or extends what's written here, update this section in the same change that fixes the code, with a one-line note on what evidence prompted it. Treat a mismatch between this section and the running code as a bug in one of the two.

### Frame structure
The search page (`search.do?wf=procFcstVMISearch...`) typically loads as one of ~9 frames on the outer `home.do` page:
- Frame 0: `home.do` — the main shell/nav, not the search form.
- The actual search form frame — its URL contains `wf=procFcstVMISearch`. Its position in the frame list is **not stable across page loads** — don't hardcode a frame index.
- `HiddenForm.jsp` — carries preset filter context (`preset_filter`, `DisplayDataMeasures`, etc.) as ~200 disabled hidden inputs. Not interactive.
- Several `blank.html` frames and a `cdn-walkme.e2open.com` frame (product tour widget) — safe to ignore, `first_match()`'s frame scan already skips past these efficiently via a cheap `.count()` gate before committing to a full wait.
- `about:blank` frames — placeholders, ignore.

### Autocomplete widgets (`eto-complex-autocomplete`)
Both "Country Name" and "L5 - Product" are instances of this component, not native `<select>` elements and not static dropdowns — there is no option list until you actually type.

- **Stable identifier**: the container `id` (`#CustomerDescription__Autocomplete`, `#CustItemDescription__Autocomplete`). The inner text input's `id` (`#eto13`, `#eto29`, etc.) is dynamically numbered per page load — **never hardcode it**.
- **Text input attributes** (confirmed): `class="eto-complex-autocomplete__field"`, `delimiter="comma"`, `replacedelimiter="^^^"`. The delimiter attribute is load-bearing: a literal comma typed into this field is read as a term separator mid-keystroke, not as part of the value. See the comma-handling rule in CLAUDE.md.
- **Typing must fire real keystroke events.** `press_sequentially()`, not `fill()` — `fill()` sets the value without dispatching the events this widget listens for, leaving the results panel empty.
- **Results panel nesting is inconsistent.** Confirmed across multiple captures of the *same* widget: sometimes `.eto-results` renders as a direct child of the field's own container div; sometimes that container has no `.eto-results` descendant at all, and an identically-shaped, populated `.eto-results-available`/`.eto-results-selected` pair exists elsewhere in the same frame. Code must search the frame broadly (not `container.locator(...)`) when a nested lookup finds nothing.
- **A populated result item's real markup:**
  ```html
  <li class="eto-results__option" role="option" data-index="0"
      data-value="Kenya" data-text="Kenya" id="eto15-0">
    <label class="eto-checkbox">
      <input class="eto-checkbox__field" type="checkbox">
      <span class="eto-checkbox__box"></span>
      <span class="eto-checkbox__label">
        <span class="highlightSearchTerm">Kenya</span>
      </span>
    </label>
  </li>
  ```
  `data-value`/`data-text` give an exact match target — prefer that over fuzzy text matching.
  `.eto-checkbox__box` is the actual **visible, styled** checkbox indicator (a `<span>`); the real `<input type="checkbox">` may not be the effective click target.
- **Selected-state signal**: a genuinely-selected option gets an added class, confirmed as `eto-results-selected-option-color`, on both the "Available" list item and its counterpart in `.eto-results-selected` (there rendered as `<li class="eto-results__selected-option eto-results-selected-option-color" ...>`). Checking checkbox `checked` **and** this class together, on the same element just clicked, is the reliable immediate signal that a click actually registered — see CLAUDE.md's verification rule for why checking either alone is insufficient.
- **Hidden backing input**: `<input name="{FieldPrefix}" style="display:none" value="">` (e.g. `name="CustomerDescription"`) — this is what the real form submits, comma-joined for multiple values. **Only syncs on blur, not per selection.** It will read empty immediately after a successful click; that's expected, not a failure.
- **`Select All` / `Clear All`**: each pane has its own link — `.eto-results__select-all a` in the Available pane, `.eto-results__clear-all a` in the Selected pane. Both are present in the DOM even when empty ("No results." shown alongside), so `Clear All` is always safe to click unconditionally.

### Reset button — avoid it
`resetSearch()` (confirmed from captured JS) checks a dirty flag: if the form has changed, it shows `confirmActionModal('prompt-modal', '', 'All changes will be lost. Are you sure you want to continue?', reloadPage)`. Confirming calls `reloadPage()`, which reloads the entire search workflow via `rcptop.getWFM()` — slow, and invalidates in-flight element references. Use each widget's own `Clear All` instead; it never touches Reset, the modal, or a reload.

### Search button
Confirmed real markup:
```html
<button type="button" onclick="" id="uuid_6d6083d8-f1ea-4715-be71-e69a69674405"
        class="eto-btn eto-btn--primary" data-on-click="javascript:search(false)">
  Search
</button>
```
The `id` is a per-page-load random UUID — **never hardcode it**. The stable, precise target is the `data-on-click="javascript:search(false)"` attribute; `config.yaml`'s `search_page.search_button` selector list uses that first, falling back to `class` + text, then plain text, in case a future page load renders this differently.

### Results grid may load into a frame that doesn't exist yet
The search form's hidden context form (`HiddenForm.jsp`) has `target="rcp_content"` — confirmed evidence that results plausibly load into a frame created only after Search is clicked, not one present on the page beforehand. `first_match()`'s normal frame scan enumerates `page.frames` once per call, so it can never find a frame that doesn't exist yet at that moment, no matter how long the timeout is. The post-search wait for `results.row` passes `poll_for_new_frames=True` specifically for this reason — it re-enumerates frames on every retry instead of once. Don't set this on other calls by default: it turns "fails fast when nothing exists" into "retries for the full timeout," which is the wrong tradeoff for routine existence probes (cookie banners, optional buttons, modals).

### Collaboration Selector grid (post-search results page)
Confirmed URL: `selectCollab.do?wf=procFcstVMISearch&wff=procFcstVMISearchSelectorFineTune...`, loaded into a frame created only after Search is submitted (see the frame-timing note above). The flow is `Search Supply Planning` → click Search → `Collaboration Selector` (this page). **Confirmed real behavior: this page is meant to be select-all + View, not filtered by a "Review Status" column** — there is no such column to check per-row here. `config.yaml`'s `extraction.select_all` defaults to `true` accordingly, driving `select_all_rows()` rather than `select_approved_rows()`. "Review Status - Inventory" is a *search-form* filter field (a `<select multiple>` combobox seen earlier in a capture), not a results-grid column — `search_page.review_status_inventory_dropdown` is where that would need to be wired in if search-time status filtering is ever wanted, not column-scanning after the fact.

`select_approved_rows()` and its "Review Status - Inventory" column search are kept in the codebase for reference / in case some other grid does have a genuinely filterable status column, but they are **not the confirmed path for this page** — don't reach for them here.

The results grid uses the same `eto-checkbox` component as the autocomplete widgets — a `<label class="eto-checkbox">` wrapping `<input type="checkbox">` and a visible `<span class="eto-checkbox__box">`. `_check_eto_checkbox()` in `scraper.py` handles both per-row ticking and the select-all checkbox.

**`select_all_rows()` does not assume a `<thead>` wrapper.** An earlier version required `thead tr` specifically and failed with "Could not locate a header row" on the real portal — confirmed this grid doesn't reliably use that structure. Fixed to take the FIRST checkbox found, in DOM order, within the results table (`results.table` selector) instead — the select-all control still reliably renders before any data row's own checkbox regardless of what wraps it, so this doesn't depend on any particular table markup.

### Confirmed real Collaboration Selector markup (from an actual captured page)
A full real capture (not a guess) confirmed:
- Real column headers: **Country Name**, **Supply Plan Bucket Description**, **L3 - Method**, **L5 - Product** — no "Review Status" column in this particular table.
- Row checkbox: `<label class="eto-checkbox"><input class="eto-checkbox__field eto-row-indicator" type="checkbox" name="chk-collab-id" value="...">  <span class="eto-checkbox__box"></span></label>` — exactly the assumed pattern.
- Select-all checkbox: `<div class="eto-checkbox eto-checkbox-menu eto-all-rows-indicator"><label><input class="eto-checkbox__field" type="checkbox"><span class="eto-checkbox__box"></span></label>...` — has an attached dropdown (`eto-dropdown__menu` with "All on all pages" / "None"), but a plain click on the checkbox/box/label itself (not the dropdown arrow) is what `_check_eto_checkbox()` targets, and that's sufficient for per-page selection - no need to open the dropdown.
- The View button and Export/File Download menu (`id="fileDownloadMenu"`, `id="export"`, `onclick="javascript: doExport();"`) match exactly what was already implemented from the earlier description - confirms `results.view_button` / `results.download_icon` / `results.export_option` didn't need changing.
- The captured page also confirms "Review Status - Inventory" DOES exist as a real header elsewhere in this portal's grids (`<th ... role="columnheader"><span class="eto-grid-column__label">Review Status - Inventory</span></th>`) with the EXACT expected text - which is what led to finding the real bug below, since `select_approved_rows()`'s search should have found this text if it were looking in the right place.

### The real cause of "Could not locate 'Review Status - Inventory' column header": iframe-blindness, not wording
`select_approved_rows()`, `select_all_rows()`, and parts of `open_view()`/`back_to_results()` called `self.page.locator(...)` directly instead of going through frame-aware lookup. `self.page.locator(...)` only ever searches the main/outer document - never an iframe. Since the actual Collaboration Selector grid always loads inside an iframe (confirmed: `first_match()` with `poll_for_new_frames=True` is what actually finds `results.row` after search submission), every raw `self.page.locator(...)` call in this part of the flow was silently searching the wrong document. The real captured header text matches "Review Status - Inventory" exactly - it was never a wording problem, it just was never being looked for in the right place.

Fixed with a new `_get_grid_frame()` helper: resolves (and caches, reusing `first_match()`'s existing frame cache) whichever Page/Frame actually holds `results.row`, and every subsequent lookup in these methods is scoped to that frame instead of `self.page`. Also handles a cached frame going stale after a navigation (the same class of staleness `first_match()` already guarded against for its own cache, which `_get_grid_frame()` initially didn't) - a re-navigation between countries/pages without this check raises "Frame was detached" instead of falling back to a fresh scan.

**If you add a new method that reads from or interacts with the results grid, do not call `self.page.locator(...)` directly - call `self._get_grid_frame()` first and locate against that.** This bug class is easy to reintroduce.

### `networkidle` waits must be bounded and non-fatal
Confirmed on a real run: `back_to_results()`'s fallback (`page.go_back()` then `wait_for_load_state("networkidle")`) hit the full 60s default navigation timeout and crashed the entire multi-country pipeline - this portal's JS likely polls continuously, which can prevent `networkidle` from ever firing at all. Both `back_to_results()` and `open_gfpvan()` now pass an explicit, shorter `timeout=` and catch `PWTimeout` rather than letting it propagate - confirmed via a fixture that never reaches `networkidle` (a page firing a request every 200ms forever): the wait now correctly times out at the bounded value instead of the old 60s default, and doesn't crash the run. Failing to confirm "the page fully settled" is not worth aborting an otherwise-successful multi-country run over - the next operation's own `first_match()` calls will discover the real state regardless. Any new `wait_for_load_state("networkidle")` call added to this codebase should follow the same pattern.

### `page.goto()` can raise `net::ERR_ABORTED` even when navigation actually succeeds
Confirmed on a real run: `open_gfpvan()`'s fallback `self.page.goto(cfg.get("urls.gfpvan_home"))` raised `net::ERR_ABORTED`. This is a known Playwright/Chromium behavior, not necessarily a real failure: if the target page's own JS fires a client-side redirect before the original navigation's `load` event completes, Chromium reports the *original* request as aborted even though the browser correctly ends up on the redirected page. Verified by forcing the exact real error via `page.route(url, lambda route: route.abort('aborted'))` (which reproduces `net::ERR_ABORTED` precisely, unlike a plain client-side `window.location` redirect, which Playwright actually follows without erroring in testing). `open_gfpvan()` now catches `playwright.sync_api.Error` around this specific `goto()` call, logs the actual landing URL, and continues rather than crashing - the next step's own `first_match()` calls confirm whether landing actually failed. Any new direct `page.goto()` call to a URL that might itself redirect should use the same pattern.

### The downloaded Multi-Collab View export's real structure (confirmed from an actual downloaded `.tsv`)
This was the single biggest remaining unknown, resolved once a real user uploaded an actual staged parquet from a real run. The `.tsv` `export_results_tsv()` downloads is a classic 2-row-merged-header pivot export - naive `pd.read_csv(path, sep="\t")` (the original implementation) produces garbage (`'Details'`, `'Unnamed: 1'`...`'Unnamed: 54'`, and stray group-header text like `'Collaboration View'` and the bucket-date-range note showing up AS column names).

Real structure, confirmed directly:
- **File line 1**: group/title headers - mostly blank, with `'Collaboration View'`, `'Past Due'`, and a note like `'Default - 2026-02-01 -- 2028-08-31    All bucket dates are in system time.'` marking where blocks of real columns start. This is what `header=0` incorrectly uses as column names.
- **File line 2**: the REAL header row - static attribute names (`Country ISO Code`, `Supply Plan Bucket Description`, `L5 - Product`, `Membership Type`, `Desired/Min/Max MOS`, `Review Status - Inventory`, `Review Status - Supply Plan`, `Alert On`, `Assigned Analyst Email`/`Assigned Analyst`, upload schedule/due-date fields, `S1`-/`S2`-prefixed MOS fields - 22 columns total) followed by one column per monthly bucket (`2026-02` through `2028-08` - 31 columns, confirmed exactly matching the date-range note), then a `Total` column. `parse_mcv_tsv()` reads with `header=1` to skip line 1 and use this as the real header.
- **Data rows**: each product/collaboration spans MULTIPLE rows - one **master row** (static attributes populated, metric columns blank) followed by several **metric rows** (static attributes blank, an otherwise-unlabeled column holds a metric name like `Monthly Consumption`/`Supply Plan Inventory`/`Projected Inventory Adjustment` - 34 distinct real metric names confirmed - and the monthly columns hold that metric's value per period). `parse_mcv_tsv()` forward-fills the static attributes down through each block, then keeps only the metric rows.
- The metric-name column and one other spacer column both show up as blank/`Unnamed: N` in the real header row - `parse_mcv_tsv()` does NOT assume a fixed positional offset for this (an earlier version did, and was confirmed wrong in general - correct for one real file purely by coincidence). It scans every unnamed column before the first month column and picks whichever one actually has data, since a genuine metric-name column is populated on nearly every metric row while a true spacer column is empty.
- **`Country` in this export is the ISO code** (e.g. `KE`), not the full name (`Kenya`) used everywhere else in this pipeline (search scope, `Country Name` field, etc.). `parse_mcv_tsv()` passes it through as-is rather than guessing an ISO-to-name mapping table - add one explicitly if downstream code needs to join on full country names.
- 13 of the 34 real metric names already matched `config.yaml`'s `metric_to_column` mapping exactly (confirmed, not luck); the other 21 come through as `raw_`-prefixed columns per `reshape_to_wide()`'s existing (and correct) behavior for unmapped metrics.

Verified end-to-end against the actual real data: reconstructed the original 2-header-line `.tsv` from a user-provided staged parquet (which had the old buggy `header=0` parse already applied) and ran the real `parse_mcv_tsv()` against it - produced the correct wide shape (93 rows: 3 products × 31 periods, `Product`/`Country`/`Period` + 12 mapped metric columns + 14 `raw_`-prefixed ones) and passed `MULTI_COLLAB_SCHEMA` validation, which is exactly what was failing before ("missing columns: ['Product', 'Country', 'Period']").

### The Multi-Collab View pivot grid has its OWN select-all checkbox - distinct from the Collaboration Selector's
Confirmed from a real captured page: the pivot grid can contain MULTIPLE distinct "collab" rows (different Country/Supply-Plan-Bucket/Product combinations, each with its own numeric collab id - a real capture showed two: `1363` and `1382`, both for the same product but different buckets). Each row has its own checkbox (`<input class="eto-checkbox__field row-indicator" name="filteredCollabs" value="{collab_id}">`), and the grid's header has its own select-all control, structurally separate from the Collaboration Selector's:
```html
<th data-column="Checkbox" role="columnheader">
  ...
  <label class="eto-checkbox">
    <input class="eto-checkbox__field all-rows-indicator" type="checkbox" name="ALL">
    <span class="eto-checkbox__box"></span>
  </label>
</th>
```
Without checking this before exporting, `Export` only includes whatever's selected by default - confirmed real bug: a download that silently contained just one view while several others were present in the grid. `export_results_tsv()` now checks `multi_collab.select_all_checkbox` (`th[data-column='Checkbox']`) via the same `_check_eto_checkbox()` helper used elsewhere, before clicking the download icon. Degrades gracefully (a warning, not a crash) if this checkbox isn't found on some page variant - the export still proceeds, just with whatever's already selected.

Verified with a fixture reproducing the exact real markup (three distinct collab rows, each independently checkable, plus the `name="ALL"` select-all): confirmed the bug is real (nothing pre-selected, an immediate export would be empty), then confirmed the fix selects all three before exporting, and confirmed graceful degradation when a different page variant lacks this checkbox entirely.

### A benign `net::ERR_ABORTED` can also mean a genuinely dead session
The `open_gfpvan()` fix above assumes landing somewhere reasonable after the abort. Confirmed on a real run this isn't always true: it can land on `logon.do?sessionExpired=true` - the session was actually invalidated seconds after a fresh login, not just redirected harmlessly. `open_gfpvan()` now checks the landing URL for `logon.do`/`sessionExpired` regardless of which path got it there (launchpad click or the `goto()` fallback) and raises a specific `TransientError` naming the real problem, rather than silently continuing into a confusing downstream failure several steps later (`"None of the selectors matched: ['text=Menu'...]"`, which doesn't mention the actual cause at all). Verified by pointing `urls.gfpvan_home` directly at a URL containing `sessionExpired=true` and confirming the specific error fires with diagnostics captured.

### Every `networkidle` wait must go through `_wait_networkidle()` - no exceptions
This portal's JS can poll continuously, which can prevent `networkidle` from ever firing. A bare `self.page.wait_for_load_state("networkidle")` then hits the page's default navigation timeout (60s) and crashes the whole pipeline. This happened for real, independently, in **three separate methods** (`back_to_results()`, `open_gfpvan()`, `open_search_supply_planning()`) before all `networkidle` waits were centralized into one `_wait_networkidle(timeout_ms=15000)` helper (bounded, catches `PWTimeout`, logs and continues). Each prior fix only patched the one method that had just crashed - the same bug kept resurfacing in the next method that happened to get exercised by an actual run, since nothing stopped a future edit from adding a bare call again. There should be **zero** occurrences of `self.page.wait_for_load_state("networkidle")` outside of `_wait_networkidle()`'s own implementation - `grep -n "self.page.wait_for_load_state" src/gfpvan_pipeline/scraper.py` should return exactly one match. Verified directly: called `_wait_networkidle()` against a fixture page that fires a network request every 200ms forever, confirmed it returns after exactly the bounded 15s rather than hanging.

### CSV export uses a LONG format, confirmed to match a manually-downloaded reference export exactly
A user supplied their own manual downloads of the same Kenya data in two shapes - wide (`Country Name`/`Supply Plan Bucket Description`/`L5 - Product`/`DataMeasure` + one column per month) and long (`Country Name`/`Supply Plan Bucket Description`/`L5 - Product`/`DataMeasure`/`Date`/`Value` - one row per reading) - and confirmed the long shape is what they want CSV output to match.

Three things this required fixing, all confirmed against the real reference files, not guessed:
- **`Country` should be the full name (e.g. `Kenya`), not the ISO code (`KE`)** the `.tsv` itself provides. Both manual reference files use the full name. `parse_mcv_tsv()`/`parse_mcv_tsv_long()` now take `country` as a parameter (the value actually searched) instead of reading it from the file.
- **`DataMeasure` values are the RAW metric name, not run through `metric_to_column`.** That mapping exists specifically for the wide shape's column names (e.g. `Monthly Consumption` → `Projected Consumption`) and would rename things the manual reference export leaves untouched - confirmed its `DataMeasure` column uses names like `Projected Inventory Adjustment` unchanged.
- **`Supply Plan Bucket Description` was being silently dropped.** It's one of the real static attribute columns (forward-filled the same way `Country ISO Code`/`L5 - Product` already were) but nothing downstream had ever asked for it before.

The actual file-reading/column-identification logic (finding the metric-name column by data population, forward-filling static attributes, locating month columns) is shared between the wide and long parsers via one `_parse_mcv_long_records()` core - `parse_mcv_tsv()` pivots its output into the wide shape, `parse_mcv_tsv_long()` returns it directly. `run_download_extraction()` now returns `(wide_df, long_df)` instead of a single DataFrame - `wide_df` continues feeding the master workbook/schema validation unchanged, `long_df` feeds CSV export.

Verified precisely: reconstructed the real `.tsv` from an earlier user-provided staged parquet, ran `parse_mcv_tsv_long()` against it, and directly compared specific `(product, metric, date)` values against the actual manual long-format download - exact matches (e.g. Monthly Consumption for Feb 2026: `96927.0` in both).

### View button and the pivot table
Confirmed real markup:
```html
<button type="button" onclick="" id="goto-tab" class="eto-btn eto-btn--primary"
        data-on-click="javascript:onClickView()">
  View
</button>
```
Unlike the autocomplete widgets' random-UUID `id`s, `id="goto-tab"` looks stable here — but `data-on-click="javascript:onClickView()"` is still tried first in `config.yaml`, since it's the more precise, unambiguous target either way. Clicking View takes you to a pivot table view of the selected rows' data (this is what `multi_collab.container`/`extract_metric_grid()` target as the "Multi-Collab View").

### Exporting results: download icon, Export vs. File Download
Confirmed real UI on the pivot table page: a download icon (`<i class="md-icon">get_app</i>`) opens two options:
- **Export** — downloads a `.tsv` directly, no further navigation. Implemented as `scraper.export_results_tsv(download_dir)`, which clicks the icon, clicks Export, captures the resulting browser download via Playwright's `expect_download()`, and saves it to the given directory.
- **File Download** — navigates to `https://gfpvan.e2open.com/GFPVAN_sc/e2sc/ioDocs.do?wf=procFcstVMISearch&wff=target&CTX:applicationId=null`, where a further **Next** click downloads a `.xlsx`. **Not yet implemented** — `results.file_download_option` is configured for reference, but no code drives this path. Ask if you specifically need `.xlsx` output instead of `.tsv` (e.g. if downstream parsing turns out to need it); Export was implemented first as the simpler single-click path.

**This may be a much better extraction strategy than DOM-scraping the pivot table directly** (which is what `extract_metric_grid()`/`reshape_to_wide()` currently do) — downloading a well-structured file and parsing it with pandas would sidestep most of the fragility this whole document catalogs. **Now wired in as the default**: `config.yaml`'s `extraction.method` is `"download"`, so `main.py`'s pipeline calls `run_download_extraction()` (in `multi_collab_extract.py`) rather than the DOM-scraping path. Set it to `"scrape"` to fall back to the old path.

`run_download_extraction()` deliberately does NOT rename or reshape the downloaded columns — the real column names in an actual download have never been confirmed, so forcing a specific shape would just be another guess. It logs the columns it actually finds on the first page read; a schema validation failure reporting "missing columns" on a real run is expected and useful the first time this runs for real - it tells you the real columns to reconcile against `metric_to_column`/`MULTI_COLLAB_SCHEMA`, not a sign of a bug.

### Diagnostics gap in the post-search flow (fixed, but know the shape of the bug)
`dump_diagnostics()` was, for a long time, only ever called from inside `run_search()`'s own try/except. Once search itself started succeeding, execution moved into `select_approved_rows()` → `open_view()` → `extract_metric_grid()` — none of which called it — so a failure anywhere in that chain propagated all the way up with zero forensic capture ("html error frames are no longer being saved" was the exact symptom). Fixed by wrapping the per-page loop body in `multi_collab_extract.run_multi_collab_extraction()` in a `try/except Exception: scraper.dump_diagnostics(...); raise`. If a *new* module or function gets added to this post-search chain later, it needs the same treatment — dumping diagnostics is not automatic just because `dump_diagnostics()` exists somewhere in the codebase.

### Dropdown-overlap on the next field
These widgets keep their results popup open after a selection (to support picking more than one). The popup is a large, absolutely-positioned overlay (~500×390px in captures) that can visually cover whatever field comes next in the form. A click on that next field can get intercepted — Playwright reports `<div class="eto-results-available">...</div> ... intercepts pointer events`. Close it with `Escape` after finishing a field's selections, but only after refocusing that field's own text input first (see CLAUDE.md — Escape does nothing while focus sits on a checkbox).

### Known limitation of diagnostic captures
`dump_diagnostics()` calls `frame.content()` per frame, which serializes each element's **attributes**, not live DOM properties. For a text `<input>`, that means the *initial* `value` attribute, never what a user (or Playwright) has since typed. A capture is the right tool for DOM *structure* questions ("what does a populated result item look like", "is X nested in Y") — not for "what's currently in this box."

## Key Workflows

### Execution CLI
```bash
# run main pipeline
uv run python -m gfpvan_pipeline.main

# run main pipeline with manual reconciliation check
uv run python -m gfpvan_pipeline.main --baseline path/to/manual_download.xlsx --max-pages 2

# run frame inspection utility (planned - not yet implemented, see Module Map)
python -m gfpvan_pipeline.debug_frames --max-pages 1
```
