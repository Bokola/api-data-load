# CLAUDE.md

# Project Guidelines for gfpvan_pipeline

## Environment & Run Commands
* **Package Manager**: Use `uv` for environment management and execution.
* **Main Pipeline Execution**: `uv run python -m gfpvan_pipeline.main`
* **Debug Frames Utility**: `python -m gfpvan_pipeline.debug_frames` — **planned, not yet implemented**. Today, diagnosing a failed run means re-reading the raw HTML files `dump_diagnostics()` writes to `run_data/screenshots/*_frame*.html` by hand (see SKILL.md's "Confirmed Portal Layout" section for what to look for). This utility would automate that: load a saved diagnostic capture, list its frames, and report which one has the search form.
* **Required Environment Variables**:
  * `GFPVAN_USERNAME` (resolved via `credentials.username_env` in config)
  * `GFPVAN_PASSWORD` (resolved via `credentials.password_env` in config)

## Architecture & Configuration Architecture
* **Config Driven**: All selectors, URLs, retry policies, and timeouts live in `config.yaml`. Never hardcode selectors or target values in Python scripts.
* **Target Environment**: Tailored for the e2open-hosted GFPVAN portal (`authn.e2open.com` / `gfpvan.e2open.com`).
* **Composite Keys**: Master workbook deduplication uses composite keys: `Product`, `Country`, `Period`.

## Coding Standards & Conventions
* **Imports**: Standard library first, third-party packages second, local module imports third. Always include `from __future__ import annotations`.
* **Comments**: Always keep comments in lowercase, without dots or dashes.
* **Typing**: Enforce strict type annotations across all function arguments and returns (e.g., `list[dict]`, `Path | None`).
* **Error Handling**: Use custom pipeline exception classes (`ConfigError`, `ScraperError`, `AuthenticationError`, `TransientError`, `ExtractionValidationError`, `PipelineError`).
* **Diagnostics**: Do not swallow critical locator failures; ensure diagnostics (HTML/screenshots) are dumped on failure using `dump_diagnostics()`.
* **Selectors**: Always retrieve selectors through `cfg.selectors("section.key")` to leverage resilient fallbacks.
* **Autocomplete Handling**: Use container selectors (`#CustomerDescription__Autocomplete`, `#CustItemDescription__Autocomplete`) for typeahead fields rather than hardcoding dynamic `#eto*` inputs.
* **Data Protection**: Raw scraping payloads must land in `run_data/landing/` before schema validation or downstream transformations occur.

## Hard-Won Autocomplete Rules

These came out of a long debugging cycle against the live portal — each one fixed a real failure that looked like something else at first. See SKILL.md's "Confirmed Portal Layout" section for the DOM evidence behind each rule.

* **Never type a raw value containing a comma into an autocomplete field.** These inputs are configured with `delimiter="comma"`; a literal comma typed mid-keystroke is read as "end this term, start the next," fragmenting the search into multiple wrong, partial selections. Type only the substring before the first comma (verified distinct across every configured product name), and match/verify against the full original value.
* **Never click the page-level Reset button.** Its handler shows a confirm modal when the form is dirty, and confirming it reloads the whole search workflow. Use each autocomplete widget's own `Clear All` link instead — always click it unconditionally (it's a harmless no-op when nothing is selected); don't try to pre-check "is there anything to clear" against the backing input first (see next rule for why).
* **Never verify a selection or a clear against the hidden backing input's value immediately after a click.** That input only syncs on blur, not per-click, so reading it right away produces false negatives that trigger a redundant second click — which, on an already-checked checkbox, toggles it back off. Verify a selection using the SAME option element just clicked: it needs to both have `checked` and carry the widget's own "selected" class together. Verify a clear optimistically (the click itself, not a value read-back).
* **Don't assume the results/selected popup is nested inside its field's own container div.** Confirmed to vary across page loads — sometimes nested, sometimes rendered elsewhere in the same frame. Search the frame broadly rather than a container-scoped locator when a nested lookup comes up empty.
* **After finishing a field's selections, refocus its own text input before pressing Escape to close the dropdown.** Clicking a checkbox/label moves browser focus onto that element; the Escape-to-close handler is bound to the text input's own keydown, so Escape sent while a checkbox has focus does nothing — leaving the dropdown open to intercept the next field's click.
* **A diagnostic HTML capture cannot show an input's live typed text.** `frame.content()` serializes the initial `value` *attribute*, not the current DOM property a user's typing sets. Don't try to debug "what's currently in the box" from a capture — capture the actual traceback/log text instead when that's what's in question.