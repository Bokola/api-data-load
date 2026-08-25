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