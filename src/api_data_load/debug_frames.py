"""debug frames utility for gfpvan scraper

inspects main frame and all iframe child contexts to find gfpvan search
fields prints detected frames frame urls element counts and saves visual
debug screenshots to aid locator configuration
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .config import Config
from .logger import get_logger
from .scraper import GFPVANScraper

if TYPE_CHECKING:
    from playwright.sync_api import Frame

log = get_logger(__name__)


def parse_args() -> argparse.Namespace:
    # parse command line arguments
    parser = argparse.ArgumentParser(
        description="debug gfpvan iframe boundaries and search selectors"
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=1,
        help="maximum search results pages to test during frame inspection",
    )
    return parser.parse_args()


def inspect_frame_elements(frame: Frame, idx: int) -> dict[str, int]:
    # count key structural elements inside a given frame
    counts = {}
    selectors_to_check = [
        ("country_text", "text=/Country Name/i"),
        ("product_text", "text=/L5 - Product/i"),
        ("comboboxes", "[role='combobox']"),
        ("inputs", "input"),
        ("selects", "select"),
        ("iframes_nested", "iframe, frame"),
    ]

    for label, sel in selectors_to_check:
        try:
            counts[label] = frame.locator(sel).count()
        except Exception:  # noqa: BLE001
            counts[label] = -1

    return counts


def run_debug(max_pages: int) -> None:
    # load config using from_yaml explicitly
    cfg = Config.from_yaml("config.yaml")
    cfg.headless = False

    log.info("starting frame debug inspection with max_pages=%d", max_pages)

    with GFPVANScraper(cfg) as scraper:
        assert scraper.page is not None

        log.info("ensuring session and opening search view")
        scraper.ensure_logged_in()
        scraper.open_gfpvan()
        scraper.open_search_supply_planning()

        # wait for initial render and take baseline snapshot
        scraper.page.wait_for_timeout(5000)
        scraper.snapshot("debug_frames_search_view")

        frames: list[Frame] = scraper.page.frames
        print("\n==================================================")
        print(f" DETECTED {len(frames)} FRAME(S) ON PAGE")
        print("==================================================")

        target_frame: Frame | None = None

        for idx, frame in enumerate(frames):
            frame_name = frame.name or "<unnamed>"
            frame_url = frame.url or "<about:blank>"
            print(f"\n--- Frame [{idx}] ---")
            print(f" Name: {frame_name}")
            print(f" URL : {frame_url}")

            counts = inspect_frame_elements(frame, idx)
            for key, val in counts.items():
                print(f"   * {key}: {val}")

            if counts.get("country_text", 0) > 0:
                target_frame = frame
                print(f"   ==> MATCH FOUND: 'Country Name' exists in Frame [{idx}]")

        print("\n==================================================")
        if target_frame:
            target_id = target_frame.name or target_frame.url
            print(f" SUCCESS: Target search form located in frame: {target_id}")
            print(" Update scraper.py to scope locators to this frame context.")
        else:
            print(" WARNING: 'Country Name' not detected in any frame.")
            print(" Checking raw main page frame inner html for diagnostic clues...")
            content_snippet = scraper.page.content()[:1000]
            print(f" Main Page HTML Snippet:\n{content_snippet}")
        print("==================================================\n")


def main() -> None:
    args = parse_args()
    try:
        run_debug(max_pages=args.max_pages)
    except KeyboardInterrupt:
        log.info("frame debug interrupted by user")
        sys.exit(0)
    except Exception as e:
        log.exception("frame debug failed with error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()