"""Playwright driver: login, navigate, search, paginate, click View.

Selector resolution: each logical selector in config.yaml is a list of fallbacks.
first_match() iterates them and returns the first that resolves to a visible
locator. This keeps the script alive across minor DOM changes.

Anti-detection: browser context is aligned to a real desktop UA, en-US
locale, and Africa/Nairobi timezone; playwright-stealth patches common
automation signatures (navigator.webdriver, missing plugins, etc.); and a
saved session (storageState) is reused across runs via ensure_logged_in() so
a fresh login - and any security challenge it might trigger - only happens
when the saved session has actually expired.

Requires: pip install playwright-stealth  (or: uv add playwright-stealth)

NOTE: this module only drives the browser through login -> search -> select
approved rows -> open the Multi-Collab view. It does not extract data or write
to Excel / Fabric — see extract.py for that.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PWError,
    Frame,
    Locator,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
    sync_playwright,
)
from playwright_stealth import Stealth
from tenacity import (
    Retrying,
    retry_if_exception_type,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)

from .config import Config
from .logger import get_logger

log = get_logger(__name__)

# HTTP status codes that mean "credentials/authorization problem" - retrying
# with the same credentials will only fail the same way again, and can
# trigger an account lockout on some IdPs. Never retried.
AUTH_STATUS_CODES = {401, 403}

# Status codes worth retrying: rate limiting and server/gateway trouble that
# is plausibly transient.
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}

# A realistic, current desktop Chrome UA. Overriding the default matters
# because headless Chromium's default UA can be distinguishable; a normal
# desktop string is a safer default. Override via config (browser.user_agent)
# if you need to match a specific real machine's UA exactly.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class ScraperError(RuntimeError):
    pass


class AuthenticationError(ScraperError):
    """Credentials were rejected, or the server returned a 401/403.
    Never retried - retrying won't fix bad credentials and risks a lockout.
    """


class TransientError(ScraperError):
    """A retryable failure: timeout, unresolved selector (page likely still
    loading), or a 429/5xx from the server."""


class GFPVANScraper:
    """Thin wrapper around a Playwright page bound to GFPVAN."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pw: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.shots_dir = cfg.root / "screenshots"
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self.storage_state_path = Path(
            cfg.get("browser.storage_state_path", "./run_data/storage_state.json")
        )
        # Remembers which frame satisfied a given candidate-selector list
        # last time, keyed by the tuple of candidates. This portal has 9
        # frames on the search page, and the same 1-2 selectors (the
        # autocomplete container ids) get looked up repeatedly across a
        # multi-country run - without this, every single lookup re-scans
        # every frame from scratch, paying the FULL per-candidate timeout
        # on every frame that doesn't have it before reaching the one that
        # does (observed: ~15s for a single lookup on a 9-frame page).
        self._frame_cache: dict[tuple[str, ...], Frame] = {}

    # ---------------------------------------------------------------- lifecycle
    def __enter__(self) -> "GFPVANScraper":
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=self.cfg.headless)

        context_kwargs: dict = dict(
            viewport={"width": 1600, "height": 900},
            accept_downloads=True,
            user_agent=self.cfg.get("browser.user_agent", DEFAULT_USER_AGENT),
            locale=self.cfg.get("browser.locale", "en-US"),
            timezone_id=self.cfg.get("browser.timezone", "Africa/Nairobi"),
        )

        # Reuse a persisted session if we have one, so a valid login doesn't
        # get thrown away and re-triggered every run (repeated fresh logins
        # are themselves a signal that can trip security challenges).
        if self.storage_state_path.exists():
            log.info("Found saved session state at %s", self.storage_state_path)
            context_kwargs["storage_state"] = str(self.storage_state_path)
        else:
            log.info("No saved session state found - will need a fresh login")

        self.context = self.browser.new_context(**context_kwargs)

        if self.cfg.get("browser.stealth", True):
            Stealth().apply_stealth_sync(self.context)
            log.info("Applied playwright-stealth evasions to browser context")

        self.page = self.context.new_page()
        self.page.set_default_timeout(self.cfg.get("timeouts.action_ms", 30000))
        self.page.set_default_navigation_timeout(
            self.cfg.get("timeouts.navigation_ms", 60000)
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            self.snapshot("uncaught_exception")
        try:
            if self.context:
                self.context.close()
            if self.browser:
                self.browser.close()
            if self.pw:
                self.pw.stop()
        except Exception as e:  # noqa: BLE001 - best effort cleanup
            log.warning("Error during teardown: %s", e)

    # ---------------------------------------------------------------- utilities
    def snapshot(self, tag: str) -> Path:
        """Save full page screenshot for debugging. Returns path."""
        if not self.page:
            return Path()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.shots_dir / f"{ts}_{tag}.png"
        try:
            self.page.screenshot(path=str(path), full_page=True)
            log.info("Screenshot saved: %s", path.name)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not capture screenshot: %s", e)
        return path

    def dump_diagnostics(self, tag: str) -> None:
        """On a selector failure: save a screenshot, dump the HTML of EVERY
        frame (main page + all iframes) separately, and log every iframe
        URL present.

        page.content() alone is NOT enough - it only serializes the
        top-level document; an <iframe>'s own document is never inlined
        into it, no matter how "same site" the iframe looks. A selector
        failure inside an iframe (the common case for legacy portals like
        this one) would dump a "diagnostic" file that never contained the
        answer. Each frame gets its own numbered file here instead.
        """
        if not self.page:
            return
        self.snapshot(tag)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        frames = list(self.page.frames)
        for i, frame in enumerate(frames):
            try:
                html_path = self.shots_dir / f"{ts}_{tag}_frame{i}.html"
                html_path.write_text(frame.content(), encoding="utf-8")
                log.info(
                    "Saved frame %d HTML (url=%s) to %s", i, frame.url, html_path
                )
            except Exception as e:  # noqa: BLE001
                # A frame can be cross-origin, detached, or mid-navigation -
                # any of which makes .content() fail for that one frame.
                # Keep going so one bad frame doesn't lose the others.
                log.warning("Could not save HTML for frame %d (url=%s): %s", i, frame.url, e)

        try:
            frame_urls = [f.url for f in frames]
            log.info("Frames present on page (%d total): %s", len(frame_urls), frame_urls)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not enumerate frames: %s", e)

    def first_match(
        self,
        candidates: list[str],
        timeout_ms: int | None = None,
        search_frames: bool = True,
        poll_for_new_frames: bool = False,
    ) -> Locator:
        """Return the first locator from candidates that becomes visible.

        Only PWTimeout (selector never became visible) is treated as a soft
        miss that lets us fall through to the next candidate. Anything else
        (bad selector syntax, detached frame, etc.) is a real bug and is
        raised immediately so it does not get masked as "no selector matched".

        search_frames=True (default): if nothing matches in the main frame,
        also try every iframe on the page. Legacy enterprise apps (old
        Struts/JSP-style portals - GFPVAN's *.do URLs are a classic sign)
        very commonly render forms inside an iframe, where a main-frame-only
        locator will never find anything no matter how many selector
        variants you throw at it. Set search_frames=False for callers that
        specifically want to search only the top-level document.

        poll_for_new_frames=False (default): a single pass over
        self.page.frames as it exists right now. Correct - and fast to
        fail - for the overwhelming majority of callers, which are probing
        for something that either already exists or plainly doesn't
        (a cookie banner, an optional reset button, a modal).

        poll_for_new_frames=True: re-fetches self.page.frames on every
        retry instead of once, for the specific case of a frame that
        doesn't EXIST YET when this call starts and gets created partway
        through - confirmed relevant here: this portal's search form has
        target="rcp_content" on its hidden context form, suggesting
        results may load into a frame created only after submission. A
        single-pass scan can never find a frame that didn't exist at the
        moment it enumerated self.page.frames, no matter how long the
        timeout is - only re-enumerating over time catches that. Only use
        this for callers that are waiting on the RESULT of an action that
        might spawn a new frame, not for routine existence probes - it
        turns "fails fast" into "retries for the full timeout" when
        nothing is ever found, which is the wrong tradeoff for a probe.

        Two speed measures, both driven by a real observation: this portal
        has 9 frames on the search page, and the same 1-2 container
        selectors get looked up repeatedly across a multi-country run.
        Without either of these, a single lookup took ~15s (main frame
        miss + several empty iframes, each eating the FULL timeout before
        moving on):
        1. A per-candidates-tuple cache of which frame answered last time -
           checked first, before any scanning, since it's virtually always
           still the right frame within one page/session.
        2. During the iframe fallback scan, each frame gets a cheap
           .count() check before committing to a full wait_for() - an empty
           frame (e.g. blank.html) resolves in milliseconds this way
           instead of costing the full timeout to conclude "not here".
        """
        assert self.page is not None
        timeout = timeout_ms or self.cfg.get("timeouts.short_ms", 5000)
        frame_scan_ms = self.cfg.get("timeouts.frame_scan_ms", 1000)
        cache_key = tuple(candidates)
        last_err: Exception | None = None
        poll_interval_ms = 300
        elapsed = 0

        while True:
            cached_frame = self._frame_cache.get(cache_key)
            if cached_frame is not None:
                try:
                    for sel in candidates:
                        loc = cached_frame.locator(sel).first
                        if loc.count() > 0:
                            loc.wait_for(state="visible", timeout=timeout)
                            return loc
                except Exception:  # noqa: BLE001 - frame detached/navigated away; fall through to full scan
                    self._frame_cache.pop(cache_key, None)

            for sel in candidates:
                try:
                    loc = self.page.locator(sel).first
                    if loc.count() == 0:
                        # Same cheap gate as the iframe scan below - don't pay
                        # the full timeout for a selector that plainly isn't on
                        # the main frame at all.
                        continue
                    loc.wait_for(state="visible", timeout=timeout)
                    self._frame_cache[cache_key] = self.page.main_frame
                    return loc
                except PWTimeout as e:
                    last_err = e
                    continue

            if search_frames:
                # Re-fetching self.page.frames HERE (inside the retry loop,
                # not before it) is exactly what lets poll_for_new_frames
                # catch a frame created after this call started.
                for frame in self.page.frames:
                    if frame == self.page.main_frame or frame == cached_frame:
                        continue
                    for sel in candidates:
                        try:
                            probe = frame.locator(sel).first
                            if probe.count() == 0:
                                # Cheap, near-instant - don't pay the full
                                # timeout for a frame that plainly doesn't
                                # have this element at all.
                                continue
                            probe.wait_for(state="visible", timeout=frame_scan_ms)
                            log.info(
                                "Selector matched inside iframe (%s): %s", frame.url, sel
                            )
                            self._frame_cache[cache_key] = frame
                            return probe
                        except PWTimeout as e:
                            last_err = e
                            continue

            if not poll_for_new_frames or elapsed >= timeout:
                break
            sleep_ms = min(poll_interval_ms, timeout - elapsed)
            self.page.wait_for_timeout(sleep_ms)
            elapsed += sleep_ms

        raise ScraperError(
            f"None of the selectors matched: {candidates} (last error: {last_err})"
        )

    # ---------------------------------------------------------------- session persistence
    def ensure_logged_in(self) -> None:
        """Reuse a saved session if it's still valid; otherwise perform a
        full login and persist the resulting session for next run.

        Prefer calling this over login() directly in normal orchestration -
        it's what avoids repeated fresh logins tripping security challenges.
        """
        assert self.page is not None
        if self.storage_state_path.exists():
            log.info("Verifying saved session is still valid")
            self.page.goto(self.cfg.get("urls.gfpvan_home"))
            self.page.wait_for_load_state("networkidle")
            if self._looks_logged_in():
                log.info("Saved session is valid - skipping login")
                return
            log.info("Saved session is no longer valid - logging in fresh")

        self.login()

    def _looks_logged_in(self) -> bool:
        """Cheap heuristic: are we sitting on an authenticated page rather
        than a login redirect?"""
        assert self.page is not None
        if "/login" in self.page.url:
            return False
        try:
            self.first_match(
                self.cfg.selectors("launchpad.gfpvan_process_manager_link"),
                timeout_ms=5000,
            )
            return True
        except ScraperError:
            return False

    def _save_session(self) -> None:
        assert self.context is not None
        self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.context.storage_state(path=str(self.storage_state_path))
        log.info("Saved session state to %s", self.storage_state_path)

    # ---------------------------------------------------------------- login
    def _before_sleep(self, retry_state) -> None:
        """Log the upcoming wait in human terms before each retry sleeps."""
        wait_s = retry_state.next_action.sleep
        exc = retry_state.outcome.exception()
        log.warning(
            "Login attempt %d failed (%s: %s) - retrying in %.1f minutes",
            retry_state.attempt_number,
            type(exc).__name__,
            exc,
            wait_s / 60,
        )

    def login(self) -> None:
        """Log in to GFPVAN, retrying transient failures with a growing,
        jittered delay so retries don't fire on a suspiciously exact
        interval (5 min, then ~7.5 min, then ~11.25 min, ...).

        Authentication failures (bad credentials, 401/403) are NEVER
        retried - retrying identical credentials just fails identically
        again and risks tripping an account lockout. Only TransientError
        and PWTimeout (selector waits, unresolved page state, 429/5xx)
        are retried.
        """
        base_wait_s = self.cfg.get("retry.login.base_wait_seconds", 300)  # 5 min
        backoff_multiplier = self.cfg.get("retry.login.backoff_multiplier", 1.5)
        max_wait_s = self.cfg.get("retry.login.max_wait_seconds", 1800)  # 30 min cap
        jitter_max_s = self.cfg.get("retry.login.jitter_max_seconds", 45)
        max_attempts = self.cfg.get("retry.login.max_attempts", 4)

        retrying = Retrying(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(
                multiplier=base_wait_s, exp_base=backoff_multiplier, max=max_wait_s
            )
            + wait_random(0, jitter_max_s),
            retry=retry_if_exception_type((TransientError, PWTimeout))
            & retry_if_not_exception_type(AuthenticationError),
            reraise=True,
            before_sleep=self._before_sleep,
        )
        retrying(self._login_once)

    def _login_once(self) -> None:
        """A single login attempt. Raises AuthenticationError for
        credential/authorization failures (not retried) and TransientError
        for anything plausibly worth retrying."""
        assert self.page is not None

        log.info("Navigating to login page")
        response = self.page.goto(self.cfg.get("urls.login"))
        if response is not None:
            status = response.status
            if status in AUTH_STATUS_CODES:
                raise AuthenticationError(
                    f"Login page returned HTTP {status} - check credentials/access."
                )
            if status in TRANSIENT_STATUS_CODES:
                raise TransientError(f"Login page returned HTTP {status}.")
            if status >= 400:
                # Not an auth code and not in our known-transient set (e.g. a
                # 404 from a moved URL) - retrying blindly won't fix a wrong
                # URL, so this is a hard failure, not transient.
                raise ScraperError(f"Login page returned unexpected HTTP {status}.")

        self.page.wait_for_load_state("networkidle")

        # Cookie banner
        try:
            cookie_btn = self.first_match(
                self.cfg.selectors("login.cookie_accept_button"),
                timeout_ms=5000,
            )
            cookie_btn.click()
            self.page.wait_for_timeout(1000)
            log.info("Cookie banner accepted")
        except ScraperError:
            log.info("Cookie banner not present")

        # Email step
        try:
            log.info("Entering email address")
            email_input = self.first_match(
                self.cfg.selectors("login.email_input"),
                timeout_ms=10000,
            )
            email_input.fill(self.cfg.username)

            log.info("Clicking Continue")
            self.first_match(
                self.cfg.selectors("login.continue_button"),
                timeout_ms=10000,
            ).click()
        except ScraperError as e:
            # Selector never appeared - most likely the page was still
            # loading or rendered slowly, not a permanent break. Worth a
            # retry.
            raise TransientError(f"Email step failed: {e}") from e

        self.page.wait_for_timeout(2000)
        self.snapshot("after_email_continue")

        # Password step
        try:
            log.info("Waiting for password field")
            password_input = self.first_match(
                self.cfg.selectors("login.password_input"),
                timeout_ms=15000,
            )
            log.info("Entering password")
            password_input.fill(self.cfg.password)

            log.info("Submitting password")
            self.first_match(
                self.cfg.selectors("login.submit_button"),
                timeout_ms=10000,
            ).click()
        except ScraperError as e:
            raise TransientError(f"Password step failed: {e}") from e

        # Wait for login redirect. A timeout here is treated as a hard
        # failure unless we can positively confirm we are already logged in.
        #
        # Success detection: prefer an explicit urls.login_success_pattern
        # from config (a Playwright glob/regex you've confirmed against the
        # real portal). Without one, fall back to "the hostname changed away
        # from the login page's own host" - true for e2open's cross-subdomain
        # SSO flow (authn.e2open.com -> launchpad.e2open.com), and safer than
        # guessing a path segment: the previous hardcoded "**/launchpad/**"
        # pattern assumed "launchpad" would appear in the URL PATH, but the
        # real launchpad URL (https://launchpad.e2open.com/) has it in the
        # SUBDOMAIN instead - that pattern would never have matched.
        success_pattern = self.cfg.get("urls.login_success_pattern")
        try:
            if success_pattern:
                self.page.wait_for_url(success_pattern, timeout=30000)
            else:
                login_host = urlparse(self.cfg.get("urls.login", "")).hostname or ""
                self.page.wait_for_function(
                    "(loginHost) => location.hostname !== loginHost",
                    arg=login_host,
                    timeout=30000,
                )
        except PWTimeout:
            self.snapshot("login_redirect_failed")
            for sel in self.cfg.selectors("login.login_error_banner"):
                if self.page.locator(sel).count() > 0:
                    # Confirmed credential rejection - do not retry.
                    raise AuthenticationError("Login failed - credentials rejected.")
            # No error banner and no launchpad redirect: could be a slow
            # network or an unexpected intermediate screen (e.g. MFA).
            # Treat as transient rather than a confirmed auth failure.
            raise TransientError(
                "Login did not reach the launchpad URL and no error banner "
                "was found - possible MFA prompt, slow redirect, or "
                "unexpected screen. See screenshot 'login_redirect_failed'."
            )

        log.info("Login successful")
        self._save_session()

    # ---------------------------------------------------------------- open GFPVAN home
    def open_gfpvan(self) -> None:
        assert self.page is not None
        log.info("Opening GFPVAN Process Manager")
        # Try the direct link first; fall back to the home URL.
        try:
            link = self.first_match(
                self.cfg.selectors("launchpad.gfpvan_process_manager_link"),
                timeout_ms=8000,
            )
            link.click()
        except ScraperError:
            log.info("Launchpad link not found, navigating directly to home URL")
            try:
                self.page.goto(self.cfg.get("urls.gfpvan_home"))
            except PWError as e:
                # net::ERR_ABORTED here commonly means the target page's own
                # JS fired a client-side redirect before the original
                # navigation's load event completed - confirmed on a real
                # run. Chromium reports the ORIGINAL request as aborted even
                # when the browser correctly ends up on the redirected page,
                # so this is not necessarily a real failure. Don't treat it
                # as fatal - wait a moment and let the actual landing URL
                # (logged here, and implicitly verified by the next step's
                # own first_match calls) tell the real story.
                log.warning(
                    "goto(%s) raised %s - may just be a client-side "
                    "redirect Chromium reports as aborted. Landed on: %s",
                    self.cfg.get("urls.gfpvan_home"), e, self.page.url,
                )
                self.page.wait_for_timeout(2000)

        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            log.debug(
                "networkidle wait after opening GFPVAN timed out - continuing anyway"
            )

    # ---------------------------------------------------------------- open Search page
    def open_search_supply_planning(self) -> None:
        assert self.page is not None
        log.info("Opening Search Supply Planning")
        self.first_match(self.cfg.selectors("menu.menu_button")).click()
        self.first_match(self.cfg.selectors("menu.supply_planning_item")).click()
        # The 'Supply Planning' menu item itself navigates to a hub which contains
        # 'Search Supply Planning' - if it doesn't, click the sub-item.
        try:
            self.first_match(
                self.cfg.selectors("menu.search_supply_planning_item"),
                timeout_ms=4000,
            ).click()
        except ScraperError:
            log.debug("No 'Search Supply Planning' sub-item - assuming direct navigation")
        self.page.wait_for_load_state("networkidle")

    # ---------------------------------------------------------------- configure + run search
    def _select_dropdown_options(
        self, field_selector_key: str, options_xpath_key: str, values: list[str]
    ) -> list[str]:
        """LEGACY / UNUSED by run_search(): click-a-field-then-pick-from-a-
        static-option-list logic. Kept in case some OTHER field on the
        portal really is a plain dropdown (not confirmed either way) - the
        Country Name and L5 - Product fields turned out to be AJAX-driven
        autocomplete widgets instead (see _select_autocomplete_option),
        which is why this never worked for them: there was never a static
        option list on the page to find in the first place.
        """
        assert self.page is not None
        field = self.first_match(self.cfg.selectors(field_selector_key))
        field.click()
        self.page.wait_for_timeout(500)

        options_xpath = self.cfg.get(f"dropdown_options.{options_xpath_key}")
        if not options_xpath:
            raise ScraperError(
                f"No dropdown_options.{options_xpath_key} configured - can't "
                "enumerate individual options."
            )

        options = self.page.locator(f"xpath={options_xpath}")
        n = options.count()
        remaining = list(values)
        matched: list[str] = []

        for i in range(n):
            if not remaining:
                break
            opt = options.nth(i)
            text = opt.inner_text().strip()
            hit = next(
                (v for v in remaining if text == v or v in text or text in v),
                None,
            )
            if hit is not None:
                opt.click()
                matched.append(hit)
                remaining.remove(hit)
                self.page.wait_for_timeout(200)

        if remaining:
            log.warning(
                "Could not find dropdown option(s) for %s (field=%s) - check "
                "the spelling matches the portal's exact wording",
                remaining, field_selector_key,
            )
        log.info(
            "Selected %d/%d requested option(s) for %s",
            len(matched), len(values), field_selector_key,
        )

        # Close the dropdown so it doesn't cover the next field/button.
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(300)
        return matched

    def _dismiss_blocking_modal(self) -> bool:
        """Best-effort: close a modal dialog that's covering the page and
        intercepting clicks, before it blocks the next interaction.

        Confirmed to exist on this portal (from a live crash): a
        <div role="dialog" id="prompt-modal" class="eto-modal open"> can
        appear and swallow every subsequent click, including ones aimed at
        an iframe underneath it. What triggers it and what its dismiss
        button actually says is NOT yet confirmed - the candidates below
        are reasonable guesses covering common patterns, overridable via
        common.modal_dismiss_button in config.yaml once you've seen the
        real one (headless: false, or check the diagnostics dump that a
        failure now correctly captures).

        Never raises - this is opportunistic. If nothing matches, the next
        interaction will surface the real "element intercepts pointer
        events" error rather than this silently pretending it's handled.
        Returns True if something was dismissed.
        """
        assert self.page is not None
        dismiss_candidates = self.cfg.selectors("common.modal_dismiss_button") or [
            "#prompt-modal button:has-text('OK')",
            "#prompt-modal button:has-text('Continue')",
            "#prompt-modal button:has-text('Close')",
            "#prompt-modal button:has-text('Got it')",
            "#prompt-modal button:has-text('Dismiss')",
            ".eto-modal.open .eto-modal__close",
            ".eto-modal.open [aria-label='Close']",
            ".eto-modal.open button",
        ]
        try:
            btn = self.first_match(dismiss_candidates, timeout_ms=1500)
            btn.click()
            log.info("Dismissed a blocking modal")
            self.page.wait_for_timeout(300)
            return True
        except ScraperError:
            return False

    def _dismiss_blocking_modal(self) -> bool:
        """Check for an open eto-modal prompt dialog and try to dismiss it.
        Returns True if a modal was found and a dismiss action was attempted
        (not a guarantee it actually closed - caller should retry its click
        and let that fail again if not).

        Seen directly in a real run: clicking Reset opened
        <div role="dialog" id="prompt-modal" class="eto-modal open"> which
        then blocked EVERY subsequent click site-wide ("intercepts pointer
        events"). The exact confirm-button wording was NOT visible in any
        captured frame (the modal wasn't in the main frame, and the frame
        that actually holds it wasn't captured) - so this tries several
        common button-label patterns before falling back to Escape. If none
        of these match the real button, dump_diagnostics() (called by
        run_search on failure) will now capture the modal's own frame while
        it's actually open, which is what's needed to replace the guesses
        below with the real selector.
        """
        assert self.page is not None
        modal_selectors = self.cfg.selectors("modals.prompt_modal") or [
            "#prompt-modal.eto-modal.open",
            "#prompt-modal[role=dialog]",
            "div[role=dialog].eto-modal.open",
        ]
        modal = None
        for sel in modal_selectors:
            loc = self.page.locator(sel).first
            try:
                if loc.count() > 0 and loc.is_visible():
                    modal = loc
                    break
            except Exception:  # noqa: BLE001 - detached/cross-origin, try next
                continue

        if modal is None:
            return False

        log.warning("A blocking modal dialog is open - attempting to dismiss it")
        dismiss_selectors = self.cfg.selectors("modals.prompt_modal_dismiss") or [
            "button:has-text('OK')",
            "button:has-text('Yes')",
            "button:has-text('Continue')",
            "button:has-text('Confirm')",
            "button:has-text('Close')",
            ".eto-modal__close",
            "[aria-label='Close']",
        ]
        for sel in dismiss_selectors:
            btn = modal.locator(sel).first
            if btn.count() > 0:
                try:
                    btn.click(timeout=3000)
                    self.page.wait_for_timeout(300)
                    log.info("Dismissed modal via: %s", sel)
                    return True
                except PWTimeout:
                    continue

        log.warning(
            "Could not find a recognizable dismiss button in the modal - "
            "falling back to pressing Escape"
        )
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(300)
        return True

    def _backing_input_name(self, container_selectors: list[str]) -> str | None:
        """Derive the hidden backing <input>'s name from the container's
        own id, e.g. '#CustomerDescription__Autocomplete' ->
        'CustomerDescription'.

        Confirmed from captured JS: this widget maintains a plain hidden
        input (distinct from the visible text field, which shares the
        '__Autocomplete'-suffixed name) whose value is what actually gets
        submitted with the form - assigned via `backingEl.value = ...` in
        the widget's own code. Verifying selection against THIS is far more
        robust than trying to guess which visually-positioned popup pane
        belongs to which field: it's the exact same source of truth the
        real form submission uses, and it doesn't depend on any assumption
        about where in the DOM a popup happens to render.
        """
        for sel in container_selectors:
            if sel.startswith("#") and sel.endswith("__Autocomplete"):
                return sel[1:-len("__Autocomplete")]
        return None

    def _find_visible_pane(self, search_scope, pane_class: str, anchor: Locator | None = None) -> Locator | None:
        """Find the first visible `.{pane_class}` element, searching
        search_scope (a Page or Frame) wholesale rather than assuming it's
        nested inside a specific field's container div. `anchor` is
        currently unused (an earlier version tried to disambiguate multiple
        simultaneously-visible panes by on-screen distance to the anchor;
        that heuristic picked the WRONG pane in a real layout - see
        _find_all_visible_panes, which is what actually handles the
        multiple-visible-panes case now). Kept as a single-result
        convenience for call sites that only ever expect one match.
        """
        for pane in self._find_all_visible_panes(search_scope, pane_class):
            return pane
        return None

    def _find_all_visible_panes(self, search_scope, pane_class: str) -> list[Locator]:
        """Return every currently-visible `.{pane_class}` element,
        searching search_scope wholesale rather than assuming nesting.

        Confirmed from two different captures of the exact same widget: on
        one page load its .eto-results popup was a direct child of the
        field's own #CustomerDescription__Autocomplete container div; on
        another, that container had NO .eto-results descendant AT ALL,
        while an identically-shaped, populated .eto-results-available
        existed elsewhere in the same document. A container-scoped search
        finds nothing forever in the second case.

        An earlier version tried to pick the single "correct" pane among
        multiple visible ones using on-screen distance to the field's
        input. That's unreliable: in a real layout it favored an unrelated
        field's pane sitting slightly closer above over the correct field's
        own pane sitting slightly farther below, causing both a wrong
        result-match AND (worse) a wrong Clear-All click that silently left
        stale selections in place across searches. Callers now check ALL
        visible candidates against something authoritative (an exact
        data-value attribute match, or the backing input's value) instead
        of trusting position to pick the one to look in.
        """
        panes = search_scope.locator(f".{pane_class}")
        visible = []
        for i in range(panes.count()):
            candidate = panes.nth(i)
            try:
                if candidate.is_visible():
                    visible.append(candidate)
            except Exception:  # noqa: BLE001 - detached mid-check, skip it
                continue
        return visible

    def _appears_selected(
        self, container_selectors: list[str], search_scope, value: str, anchor: Locator | None = None
    ) -> bool:
        """Check whether `value` now counts as selected for this field -
        the real signal that the widget's own JS registered the selection.

        Primary and, when available, ONLY check: the hidden backing
        input's value (see _backing_input_name) - this is literally what
        the real form submits, so it's authoritative regardless of DOM
        layout or which of several visible panes happens to be near which
        field. Falls back to scanning every visible Selected pane for
        `value` only when there's no backing input to check at all.

        Deliberately NOT trusting the checkbox's raw 'checked' DOM property:
        Playwright's .check() has its own fallback behavior that can force
        that property true even when a real click wouldn't have worked
        (confirmed against a deliberately-broken fixture - the checkbox
        read back as checked, but neither the backing input nor the
        Selected pane ever actually updated).
        """
        backing_name = self._backing_input_name(container_selectors)
        if backing_name:
            backing_input = search_scope.locator(f"input[name={json.dumps(backing_name)}]").first
            if backing_input.count() > 0:
                try:
                    current_value = backing_input.input_value()
                except Exception:  # noqa: BLE001
                    current_value = None
                if current_value:
                    parts = [p.strip() for p in current_value.split(",")]
                    return value in parts or value in current_value
                return False  # backing input exists and is authoritative - empty means not selected

        safe_value = json.dumps(value)
        for selected_pane in self._find_all_visible_panes(search_scope, "eto-results-selected"):
            if selected_pane.locator(f"[data-value={safe_value}]").count() > 0:
                return True
            if selected_pane.locator("*").filter(has_text=value).count() > 0:
                return True
        return False

    def _select_autocomplete_option(self, container_selectors: list[str], value: str) -> bool:
        """Type `value` into an eto-complex-autocomplete widget's text input
        and select the matching suggestion once the AJAX-driven results
        panel populates. Returns True if a match was found and selected.

        container_selectors is a fallback list (like every other selector
        in this codebase), resolved via first_match() - not a single string.

        This is the REAL interaction the portal's Country Name / L5 -
        Product fields need - they're eto.ComplexAutocomplete widgets
        (confirmed from a captured page: the container has a stable id like
        #CustomerDescription__Autocomplete, wrapping a text
        input.eto-complex-autocomplete__field and a results panel that
        shows "No results." until a query resolves). There is no static
        option list to click - you have to actually drive the typeahead.

        press_sequentially (not fill()) is used deliberately: this widget
        listens for real keystroke events to trigger its search, and fill()
        sets the value directly without firing those events, which would
        silently leave the results panel empty.

        Searches EVERY visible .eto-results-available pane (via
        _find_all_visible_panes) for an EXACT data-value match, rather than
        guessing which single pane is "the right one" by position - see
        that method's docstring for why a position-based guess is
        unreliable. An exact attribute match on a specific, full country/
        product name is authoritative regardless of which visible pane it
        turns up in.

        Matching + selecting, confirmed from a captured page: a result
        renders as <li class="eto-results__option" role="option"
        data-value="Kenya" data-text="Kenya"> wrapping <label
        class="eto-checkbox"><input type="checkbox">...</label>. Two things
        follow from that:
        1. Match on the exact data-value attribute first - far more
           reliable than fuzzy text matching, and this markup gives us an
           exact key to match on for the first time.
        2. Click the checkbox's own <label>, not the outer <li> - clicking
           the li's padding (an earlier version's behavior) can silently
           miss whatever the actual selection handler is bound to.
        A broader text-based fallback is kept in case the exact data-value
        markup isn't what actually renders for some entries.
        """
        assert self.page is not None
        self._dismiss_blocking_modal()
        container = self.first_match(container_selectors)
        search_scope = self._frame_cache.get(tuple(container_selectors), self.page)

        text_input = container.locator(
            "input.eto-complex-autocomplete__field, input[type=text]"
        ).first
        try:
            text_input.click()
        except PWTimeout:
            if self._dismiss_blocking_modal():
                text_input.click()
            else:
                raise
        text_input.fill("")
        # Type only the portion before the first literal comma, NOT the
        # full value. Confirmed from a captured page: this input has
        # delimiter="comma" (replacedelimiter="^^^") configured - every
        # comma we type gets interpreted as "end this term, start the
        # next", not as part of one value. Every product name in this
        # search scope contains commas, and typing them raw produced a
        # cascade of fragmented, WRONG selections (confirmed: captures
        # showed things like "Medroxyprogesterone Acetate 104 mg/0.65 mL"
        # and even a bare " Subcutaneous" selected on their own, split mid-
        # keystroke, none matching the actual intended product). The
        # portion before the first comma is still distinct enough to
        # filter to exactly one match for every value in this list -
        # verified against config.yaml's actual product/country names.
        # The FULL original value (with commas) is still what gets matched
        # exactly against data-value below, and what gets logged/returned -
        # only the typed query is shortened.
        search_query = value.split(",")[0].strip()
        text_input.press_sequentially(search_query, delay=50)

        safe_value = json.dumps(value)  # CSS-safe quoting for the attribute selector

        wait_ms = self.cfg.get("timeouts.autocomplete_results_ms", 8000)
        poll_ms = 200
        elapsed = 0
        option = None
        while elapsed < wait_ms:
            panes = self._find_all_visible_panes(search_scope, "eto-results-available")
            for available_pane in panes:
                exact = available_pane.locator(
                    f"li.eto-results__option[data-value={safe_value}]"
                )
                if exact.count() > 0:
                    option = exact.first
                    break
            if option is None:
                for available_pane in panes:
                    candidates = available_pane.locator(
                        "[role=option], li, .eto-results__item, a, span"
                    ).filter(has_text=value)
                    if candidates.count() > 0:
                        option = candidates.first
                        break
            if option is not None:
                break
            self.page.wait_for_timeout(poll_ms)
            elapsed += poll_ms

        if option is None:
            log.warning(
                "No autocomplete suggestion appeared for '%s' in %s within %dms",
                value, container_selectors, wait_ms,
            )
            return False

        checkbox_label = option.locator("label.eto-checkbox, .eto-checkbox").first
        checkbox = option.locator("input[type=checkbox]").first
        checkbox_box = option.locator(".eto-checkbox__box").first
        selected = False

        def option_shows_selected() -> bool:
            """Check the SAME option element we just clicked for direct
            evidence of selection, rather than searching elsewhere on the
            page - confirmed from a captured page: a genuinely-selected
            item gets an added class (seen: 'eto-results-selected-option-
            color') AND its checkbox carries checked="true" as a real
            attribute, together, immediately on click. Requiring BOTH
            (not just checkbox.is_checked() alone) matters: a deliberately-
            broken test fixture showed Playwright's .check() can force the
            checked property even when nothing genuinely registered - that
            fixture never added a selected class, so requiring both
            correctly rejects it while still accepting the real portal's
            immediate, synchronous response to a click.
            """
            try:
                class_attr = option.get_attribute("class") or ""
            except Exception:  # noqa: BLE001
                class_attr = ""
            has_selected_class = "selected" in class_attr.lower()
            is_checked = False
            if checkbox.count() > 0:
                try:
                    is_checked = checkbox.is_checked()
                except Exception:  # noqa: BLE001
                    is_checked = False
            return has_selected_class and is_checked

        # Layered attempts. option_shows_selected() is checked FIRST and is
        # immediate (no dependency on the backing input, which a captured
        # page showed can still read empty right after a successful click -
        # it apparently only syncs later, e.g. on blur/close, not per
        # checkbox toggle). Checking too early against the backing input
        # produced a false "not selected" that caused a redundant SECOND
        # click - which, on an already-checked checkbox, toggles it back
        # OFF. Only fall through to the backing-input/Selected-pane check
        # (_appears_selected) as a last resort if the direct signal never
        # appears, and only escalate to another click attempt if it
        # doesn't. .check() is Playwright's purpose-built checkbox action
        # and is tried first; falling back to clicking .eto-checkbox__box -
        # confirmed from a captured page this is the actual VISIBLE, styled
        # checkbox indicator (a <span>, not the <input> itself) - then the
        # label, then the option itself, covers cases where .check()'s
        # stricter actionability requirements don't hold on this portal for
        # some reason not visible in a static capture.
        if checkbox.count() > 0:
            try:
                checkbox.check(timeout=3000)
            except PWTimeout:
                pass
            selected = option_shows_selected()

        if not selected and checkbox_box.count() > 0:
            checkbox_box.click()
            selected = option_shows_selected()

        if not selected and checkbox_label.count() > 0:
            checkbox_label.click()
            selected = option_shows_selected()

        if not selected:
            option.click()
            selected = option_shows_selected()

        if not selected:
            selected = self._appears_selected(container_selectors, search_scope, value, text_input)

        if not selected:
            log.warning(
                "Clicked suggestion '%s' in %s but it does not appear in "
                "the Selected pane afterward - selection did not register. "
                "Treating this as a failed match rather than a silent "
                "false success.",
                value, container_selectors,
            )
            return False

        self.page.wait_for_timeout(300)
        log.info("Selected '%s' via autocomplete in %s", value, container_selectors)
        # Refocus the field's own text input before returning - clicking
        # the checkbox/label/option almost certainly shifted browser focus
        # onto that element instead. A caller pressing Escape right after
        # this (to close the dropdown before touching a different field)
        # needs focus to be back on the text input first: this widget's
        # (and this fixture's, confirmed) Escape-to-close handling is bound
        # to the input's own keydown, not the checkbox's - Escape sent
        # while focus sits on the checkbox does nothing, leaving the
        # dropdown open to intercept the next field's click.
        try:
            text_input.focus()
        except Exception:  # noqa: BLE001
            pass
        return True

    def _clear_autocomplete_selections(self, container_selectors: list[str]) -> None:
        """Clear all currently-selected values in one eto-complex-autocomplete
        widget via its own 'Clear All' link, confirmed real markup:
        .eto-results-selected .eto-results__clear-all a (seen directly in a
        captured page, present even when nothing is selected yet - "No
        results." shown alongside it - so it's always safe to click
        unconditionally, with no need to check first whether there's
        "anything to clear").

        Deliberately NOT using the page-level Reset button: its handler
        (resetSearch(), confirmed from captured JS) pops a confirm modal
        when the form is dirty - 'All changes will be lost. Are you sure
        you want to continue?' - and confirming it calls reloadPage(),
        which reloads the whole search workflow. Clearing per-widget
        sidesteps the modal and the reload entirely.

        ALWAYS clicks - no pre-check via the backing input. An earlier
        version skipped clicking when the backing input already read
        empty, reasoning "nothing to clear" - that's wrong: a captured
        page proved the backing input only syncs on blur, not per
        selection, so it can read empty even when something genuinely IS
        still selected internally. That false "nothing to clear" signal
        skipped a real clear, and the next selection then accumulated with
        the stale one instead of replacing it - a duplicate-row bug that
        only surfaced two searches into a run. Clicking is a no-op when
        nothing is selected, so there's no cost to doing it every time.

        Tries the field's own nested Clear All first (the common/reliable
        case - confirmed present in every capture so far). Only scans the
        whole frame for any other visible Clear All link if nested truly
        doesn't exist, which happens specifically when this field's popup
        isn't nested in its container (see _find_all_visible_panes) - in
        that situation only one popup is genuinely open at a time, so
        "whichever Clear All is visible" is unambiguous.
        """
        assert self.page is not None
        container = self.first_match(container_selectors)
        search_scope = self._frame_cache.get(tuple(container_selectors), self.page)

        text_input = container.locator(
            "input.eto-complex-autocomplete__field, input[type=text]"
        ).first
        try:
            text_input.click(timeout=3000)
        except PWTimeout:
            pass  # fall through and try anyway

        nested = container.locator(
            ".eto-results-selected .eto-results__clear-all a, .eto-results__clear-all a"
        )
        if nested.count() > 0:
            try:
                nested.first.click(timeout=2000)
                self.page.wait_for_timeout(200)
                log.info("Clicked Clear All in %s", container_selectors)
                return
            except PWTimeout:
                log.debug(
                    "Nested Clear All in %s wasn't clickable - trying frame-wide", container_selectors
                )

        clicked = False
        for link in search_scope.locator(".eto-results__clear-all a").all():
            try:
                if link.is_visible():
                    link.click(timeout=2000)
                    self.page.wait_for_timeout(200)
                    clicked = True
                    break
            except Exception:  # noqa: BLE001
                continue

        if clicked:
            log.info("Clicked Clear All (frame-wide fallback) in %s", container_selectors)
        else:
            log.warning("Could not find any Clear All link to click for %s", container_selectors)

    def run_search(self, country: str, products: list[str]) -> None:
        """Restrict the search to ONE country and a specific list of L5
        products (rather than 'Select All' on both fields), then submit.

        Clears any leftover selection in each autocomplete widget via its
        own 'Clear All' link before selecting - see
        _clear_autocomplete_selections for why the page-level Reset button
        is deliberately avoided here.

        On any ScraperError, dumps diagnostics (screenshot + HTML + frame
        list) before re-raising - a selector failure here is exactly the
        kind of thing that's expensive to reproduce, so capture the evidence
        the first time rather than requiring a re-run.
        """
        assert self.page is not None

        try:
            country_container = self.cfg.selectors("search_page.country_name_container")
            if not country_container:
                raise ScraperError(
                    "config.yaml is missing selectors.search_page.country_name_container "
                    "(the eto-complex-autocomplete widget's container id)"
                )
            product_container = self.cfg.selectors("search_page.l5_product_container")
            if not product_container:
                raise ScraperError(
                    "config.yaml is missing selectors.search_page.l5_product_container "
                    "(the eto-complex-autocomplete widget's container id)"
                )

            self._clear_autocomplete_selections(country_container)
            self._clear_autocomplete_selections(product_container)

            log.info("Selecting country: %s", country)
            if not self._select_autocomplete_option(country_container, country):
                raise ScraperError(f"Country '{country}' did not match any autocomplete suggestion")
            # Close the country dropdown before touching the product field.
            # These widgets keep the results popup open after a selection
            # (by design - it supports picking more than one), and a
            # captured page showed this results panel as a large, absolutely-
            # positioned overlay - if it visually overlaps the product
            # field, a click there can get intercepted by country's still-
            # open panel instead of landing on the product input (same
            # class of problem as the confirm-modal blocking issue, just a
            # different overlay). Escape reliably closes this style of
            # dropdown without submitting or navigating anything.
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(200)

            log.info("Selecting %d L5 product(s)", len(products))
            matched_products = [
                p for p in products if self._select_autocomplete_option(product_container, p)
            ]
            if not matched_products:
                raise ScraperError("None of the requested L5 products matched any autocomplete suggestion")
            if len(matched_products) < len(products):
                log.warning(
                    "Only %d/%d requested products matched - missing: %s",
                    len(matched_products), len(products),
                    [p for p in products if p not in matched_products],
                )
            # Close the product dropdown too, before clicking Search - same
            # reasoning as above.
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(200)

            log.info("Submitting search for %s", country)
            self.first_match(
                self.cfg.selectors("search_page.search_button")
            ).click()
            self.page.wait_for_load_state("networkidle")
            try:
                # poll_for_new_frames=True here specifically: the search
                # form's own hidden context form targets target="rcp_content"
                # (confirmed from a captured page), suggesting results may
                # load into a frame that doesn't exist yet at the moment
                # Search is clicked. A single-pass frame scan can never find
                # a frame created after it started, no matter the timeout -
                # this is the one call in the whole flow where that
                # distinction actually matters, since every other lookup
                # targets frames already present on the page.
                self.first_match(
                    self.cfg.selectors("results.row"),
                    timeout_ms=15000,
                    poll_for_new_frames=True,
                )
            except ScraperError:
                log.warning(
                    "No result rows appeared after search submission for %s "
                    "(may just mean no matching records exist)", country,
                )
                # This is genuinely uncharted territory - the results grid's
                # real markup has never been captured. Dump diagnostics here
                # too (not just on a hard failure) so a "zero rows" run
                # still leaves forensic evidence instead of only a guess.
                self.dump_diagnostics(f"no_results_after_search_{country}")
        except (ScraperError, PWTimeout) as e:
            # PWTimeout (e.g. a raw Locator.click() timeout) is caught here
            # too, not just ScraperError - without this, an interaction
            # timeout (as opposed to first_match's own controlled "no
            # selector matched" ScraperError) would propagate straight past
            # this diagnostic capture, and main.py's per-country error
            # handling wouldn't catch it either since it only expects
            # ScraperError. Wrapping it keeps both behaviors uniform: every
            # run_search failure gets diagnostics dumped AND gets treated
            # as "skip this country, continue with the next" rather than
            # crashing the whole run.
            self.dump_diagnostics(f"run_search_failed_{country}")
            if isinstance(e, ScraperError):
                raise
            raise ScraperError(f"Unexpected error searching for {country}: {e}") from e

    def run_search_approved_only(self) -> None:
        """Select all countries, select all L5 products, then click Search.

        Kept for reference / ad-hoc use - the pipeline's normal path is
        run_search() with an explicit country + product list instead, since
        that's what config.yaml's search_scope actually describes.
        """
        assert self.page is not None

        log.info("Selecting all countries")
        country = self.first_match(
            self.cfg.selectors("search_page.country_name_field")
        )
        country.click()
        self.page.get_by_text("Select All", exact=True).first.click()
        self.page.wait_for_timeout(1000)

        log.info("Selecting all L5 products")
        product = self.first_match(
            self.cfg.selectors("search_page.l5_product_field")
        )
        product.click()
        self.page.get_by_text("Select All", exact=True).last.click()
        self.page.wait_for_timeout(1000)

        log.info("Submitting search")
        self.first_match(
            self.cfg.selectors("search_page.search_button")
        ).click()
        self.page.wait_for_load_state("networkidle")
        # Belt-and-braces: also wait for a concrete results marker, since
        # networkidle can fire before a heavy grid finishes rendering.
        try:
            self.first_match(
                self.cfg.selectors("results.row"), timeout_ms=15000
            )
        except ScraperError:
            log.warning("No result rows appeared after search submission")

    # ---------------------------------------------------------------- pagination helpers
    def total_pages(self) -> int:
        """Read 'Page X of Y' indicator. Returns 1 if not found."""
        assert self.page is not None
        for sel in self.cfg.selectors("results.page_indicator"):
            loc = self.page.locator(sel).first
            if loc.count() > 0:
                txt = loc.inner_text()
                # "Page 1 of 2 ; 43 Records"
                m = re.search(r"Page\s+(\d+)\s+of\s+(\d+)", txt)
                if m:
                    return int(m.group(2))
        return 1

    def goto_next_page(self) -> bool:
        """Click 'next page'. Returns False if no candidate resolves to an
        enabled control."""
        assert self.page is not None
        for sel in self.cfg.selectors("results.next_page_button"):
            loc = self.page.locator(sel).first
            if loc.count() == 0:
                continue
            if not loc.is_enabled():
                # Don't give up yet - a later fallback selector might point
                # at the real, enabled control.
                continue
            loc.click()
            self.page.wait_for_load_state("networkidle")
            return True
        return False

    # ---------------------------------------------------------------- selecting approved rows
    def _find_column_index(self, header_cells, keywords: list[str]) -> int | None:
        """Return the index of the first header cell whose text contains any
        of the given keywords (case-insensitive substring match)."""
        n = header_cells.count()
        for i in range(n):
            text = header_cells.nth(i).inner_text().strip().lower()
            if any(kw.lower() in text for kw in keywords):
                return i
        return None

    def _check_eto_checkbox(self, scope: Locator, checkbox_selectors: list[str] | None = None) -> bool:
        """Check a custom eto-checkbox component within `scope` (a row, a
        header row, or any container with exactly one relevant checkbox).

        Confirmed real markup on the post-search results grid: the same
        eto-checkbox component used by the autocomplete widgets - a
        <label class="eto-checkbox"> wrapping <input type="checkbox">
        and a visible, styled <span class="eto-checkbox__box">. Layers the
        same attempts that turned out to matter there: .check() on the raw
        input first, then the visible .eto-checkbox__box span (the actual
        styled indicator - the raw input may not be the effective click
        target), then the wrapping label. Returns True once the input
        actually reads as checked, not just because a click didn't raise.
        """
        assert self.page is not None
        selectors = checkbox_selectors or ["input[type=checkbox]"]
        checkbox = None
        for sel in selectors:
            candidate = scope.locator(sel).first
            if candidate.count() > 0:
                checkbox = candidate
                break
        if checkbox is None:
            return False
        if checkbox.is_checked():
            return True

        try:
            checkbox.check(timeout=2000)
            if checkbox.is_checked():
                return True
        except PWTimeout:
            pass

        box = scope.locator(".eto-checkbox__box").first
        if box.count() > 0:
            box.click()
            if checkbox.is_checked():
                return True

        label = scope.locator("label.eto-checkbox, .eto-checkbox").first
        if label.count() > 0:
            label.click()
            if checkbox.is_checked():
                return True

        return checkbox.is_checked()

    def _get_grid_frame(self):
        """Return whichever Page/Frame currently holds the results grid.

        Confirmed real bug: select_approved_rows() and select_all_rows()
        used self.page.locator(...) directly, which only searches the
        main/outer page - the actual grid (e.g. selectCollab.do) always
        loads inside an iframe (confirmed: first_match() with
        poll_for_new_frames=True is what actually finds results.row after
        search submission). A raw self.page.locator(...) call in the same
        flow silently searches the wrong document and finds nothing, no
        matter how correct the selector or search text is - this was the
        real explanation for "Could not locate 'Review Status - Inventory'
        column header" even though that exact header text does exist in a
        real capture: it was never being looked for in the right place.

        Resolved via the SAME frame cache first_match() already populates
        for results.row, so this doesn't cost a second full scan once
        anything has already been found once this session.

        Verifies the cached frame is still alive before trusting it - a
        page navigation (e.g. goto_next_page(), or re-navigating between
        countries) can detach a previously-cached frame, and using it
        without checking raises 'Frame was detached' instead of falling
        back to a fresh scan. first_match() already guards its own cache
        the same way; this mirrors that.
        """
        assert self.page is not None
        row_selectors = self.cfg.selectors("results.row")
        cache_key = tuple(row_selectors)
        cached = self._frame_cache.get(cache_key)
        if cached is not None:
            try:
                for sel in row_selectors:
                    cached.locator(sel).count()
                return cached
            except Exception:  # noqa: BLE001 - detached/navigated away
                self._frame_cache.pop(cache_key, None)
        try:
            self.first_match(row_selectors, timeout_ms=self.cfg.get("timeouts.short_ms", 5000))
        except ScraperError:
            pass
        return self._frame_cache.get(cache_key, self.page)

    def select_approved_rows(self) -> list[dict]:
        """Tick checkboxes for rows whose Review Status - Inventory is approved.

        Returns a list of context dicts, one per ticked row, e.g.
        {"row_index": 3, "product": "...", "country": "..."}. Pass this list
        into multi_collab_extract.extract_metric_grid() after open_view() -
        the Multi-Collab grid it opens doesn't reliably repeat Product/
        Country on every row, so it's read here, off the row that was ticked
        to get there, instead.
        """
        assert self.page is not None
        grid = self._get_grid_frame()
        approved = set(self.cfg.get("approved_statuses", ["Ready to Use Approved"]))

        header_cells = grid.locator("thead th, [role=columnheader]")
        review_col_idx = self._find_column_index(
            header_cells, ["Review Status - Inventory"]
        )
        product_col_idx = self._find_column_index(
            header_cells, ["L5 - Product", "L5 Product", "Product"]
        )
        country_col_idx = self._find_column_index(
            header_cells, ["Country Name", "Country"]
        )

        if review_col_idx is None:
            actual_headers = [
                header_cells.nth(i).inner_text().strip()
                for i in range(header_cells.count())
            ]
            log.warning(
                "Could not locate 'Review Status - Inventory' column header. "
                "Actual header cells found: %s. This may mean the column "
                "genuinely isn't in this grid (review status could be a "
                "search-time filter field instead, not a results column - "
                "see search_page.review_status_inventory_dropdown in "
                "config.yaml) rather than a wording mismatch.",
                actual_headers,
            )
            self.dump_diagnostics("review_status_column_not_found")
            return []
        if product_col_idx is None or country_col_idx is None:
            log.warning(
                "Could not locate Product/Country column header(s) - ticked "
                "rows will be returned without that context (product=%s, "
                "country=%s)", product_col_idx, country_col_idx,
            )

        row_selector = None
        for sel in self.cfg.selectors("results.row"):
            if grid.locator(sel).count() > 0:
                row_selector = sel
                break
        if row_selector is None:
            log.warning("Could not locate any result rows")
            return []

        checkbox_selectors = self.cfg.selectors("results.row_checkbox") or [
            "input[type=checkbox]"
        ]

        rows = grid.locator(row_selector)
        contexts: list[dict] = []
        # Re-check the live count each iteration in case checking a box
        # causes the grid to re-render (e.g. virtualized grids).
        r = 0
        while r < rows.count():
            row = rows.nth(r)
            cells = row.locator("td, [role=gridcell]")
            if cells.count() <= review_col_idx:
                r += 1
                continue
            status_text = cells.nth(review_col_idx).inner_text().strip()
            if status_text in approved:
                if self._check_eto_checkbox(row, checkbox_selectors):
                    contexts.append(
                        {
                            "row_index": r,
                            "product": (
                                cells.nth(product_col_idx).inner_text().strip()
                                if product_col_idx is not None
                                and cells.count() > product_col_idx
                                else None
                            ),
                            "country": (
                                cells.nth(country_col_idx).inner_text().strip()
                                if country_col_idx is not None
                                and cells.count() > country_col_idx
                                else None
                            ),
                        }
                    )
            r += 1
        log.info("Ticked %d approved rows on current page", len(contexts))
        return contexts

    def select_all_rows(self) -> int:
        """Select every row on the current results page via the grid's
        select-all checkbox, rather than ticking rows individually by
        status.

        Confirmed real UI: 'select all by clicking the first checkbox in
        the same row as the headers.' Implemented as the FIRST checkbox
        found, in DOM order, within the results table - not by first
        locating a <thead> wrapper. An earlier version required <thead tr>
        specifically and failed with 'Could not locate a header row' on
        the real portal - this grid apparently doesn't use that literal
        structure. The select-all control still reliably renders before
        any data row's own checkbox regardless of what wraps it, so "first
        checkbox in the table" is a more robust target than assuming any
        particular table markup.

        Returns the number of rows on the page (best-effort - the actual
        per-row checked state isn't re-verified individually here, since
        the select-all control is a single widget covering all of them).
        """
        assert self.page is not None
        grid = self._get_grid_frame()

        table_selector = None
        for sel in self.cfg.selectors("results.table"):
            if grid.locator(sel).count() > 0:
                table_selector = sel
                break
        scope = grid.locator(table_selector) if table_selector else grid

        checkbox_selectors = self.cfg.selectors("results.select_all_checkbox") or [
            "input[type=checkbox]"
        ]
        if not self._check_eto_checkbox(scope, checkbox_selectors):
            log.warning("Could not check the select-all checkbox")
            self.dump_diagnostics("select_all_checkbox_not_found")
            return 0

        row_selector = None
        for sel in self.cfg.selectors("results.row"):
            if grid.locator(sel).count() > 0:
                row_selector = sel
                break
        count = grid.locator(row_selector).count() if row_selector else 0
        log.info("Selected all %d row(s) via header checkbox", count)
        return count

    def open_view(self) -> bool:
        """Click View; return True if the pivot table / Multi-Collab view opened.

        Confirmed real button (id is stable here, unlike the autocomplete
        widgets' random UUIDs, but data-on-click is still the more precise
        target): id="goto-tab", data-on-click="javascript:onClickView()".
        config.yaml's results.view_button tries that first, falling back to
        plain text/role matches.
        """
        assert self.page is not None
        try:
            self.first_match(self.cfg.selectors("results.view_button")).click()
        except ScraperError as e:
            log.error("Could not click View: %s", e)
            self.dump_diagnostics("view_click_failed")
            return False
        except PWTimeout as e:
            log.error("View button click timed out: %s", e)
            self.dump_diagnostics("view_click_failed")
            return False

        # Wait for either the Multi-Collab marker or a network idle.
        # first_match() here (not raw self.page.locator) so this correctly
        # searches whatever frame the pivot view actually renders in - see
        # _get_grid_frame()'s docstring for why a raw self.page call would
        # silently search the wrong document.
        try:
            self.first_match(self.cfg.selectors("multi_collab.container"), timeout_ms=8000)
            return True
        except ScraperError:
            pass
        self.page.wait_for_load_state("networkidle")
        return True

    def export_results_tsv(self, download_dir: Path | str) -> Path:
        """From the pivot table view, click the download icon then
        'Export' to get a direct .tsv download. Returns the path the file
        was saved to.

        Confirmed real UI: a download icon (<i class="md-icon">get_app</i>)
        opens two options - 'Export' (direct .tsv download, used here) and
        'File Download' (navigates to ioDocs.do, then a 'Next' click gets a
        .xlsx). Export is implemented here since it's a single click with
        no extra page navigation; File Download -> ioDocs.do is documented
        in SKILL.md but not yet implemented - ask if you need that path
        instead (e.g. if downstream parsing specifically needs .xlsx).
        """
        assert self.page is not None
        download_dir = Path(download_dir)
        download_dir.mkdir(parents=True, exist_ok=True)

        self.first_match(self.cfg.selectors("results.download_icon")).click()
        export_option = self.first_match(self.cfg.selectors("results.export_option"))

        with self.page.expect_download() as download_info:
            export_option.click()
        download = download_info.value

        dest = download_dir / download.suggested_filename
        download.save_as(str(dest))
        log.info("Downloaded results export to %s", dest)
        return dest

    def back_to_results(self) -> None:
        """Return to the Collaboration Selector results page.

        networkidle waits here are bounded and non-fatal: confirmed on a
        real run that go_back()'s wait_for_load_state("networkidle") hit
        the full 60s navigation timeout and crashed the entire multi-
        country pipeline - this portal's JS likely polls continuously,
        which can prevent networkidle from ever firing at all. Failing to
        confirm "the page fully settled after going back" is not worth
        aborting an otherwise-successful run over: the next page's own
        first_match() calls will discover the real state regardless.
        """
        assert self.page is not None
        try:
            self.first_match(
                self.cfg.selectors("multi_collab.back_button"), timeout_ms=5000
            ).click()
            try:
                self.page.wait_for_load_state("networkidle", timeout=15000)
            except PWTimeout:
                log.debug(
                    "networkidle wait after clicking back timed out - continuing anyway"
                )
            return
        except ScraperError:
            pass
        # Fallback: browser back
        log.info("No back link found - using browser history")
        self.page.go_back()
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            log.warning(
                "networkidle wait after go_back() timed out - this portal's "
                "JS may poll continuously, preventing networkidle from ever "
                "firing. Continuing anyway; the next operation's own "
                "first_match() will confirm the real page state."
            )


def open_browser(cfg: Config) -> GFPVANScraper:
    """Sugar for `with open_browser(cfg) as s:`."""
    return GFPVANScraper(cfg)
