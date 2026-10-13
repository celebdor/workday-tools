"""Scrape the team absence calendar from Workday.

IMPORTANT — selectors are tenant-specific
-----------------------------------------
Workday renders a heavily-nested, obfuscated SPA, and the exact markup of the
absence/time-off calendar differs between tenants and between views (calendar
grid vs. table report). The parsing in ``_parse_events`` below is therefore a
*starting point* keyed off common Workday patterns; it almost certainly needs
adjusting to your view.

Workflow to wire it up:
    1. Run `python -m wdtools snapshot` — it opens your absence_calendar_url with
       the saved session and dumps the rendered HTML + a screenshot to ./snapshots.
    2. Inspect that HTML to find the elements/attributes carrying each absence
       (person, type, dates).
    3. Update SELECTORS and _parse_events accordingly.

If your tenant offers an iCal / "Export to Calendar" feed on this view, prefer
that over scraping entirely — it is far more stable. See README.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from playwright.sync_api import BrowserContext, Page

from .config import Config
from .models import AbsenceEvent

# Give the SPA time to finish its async render after navigation.
RENDER_SETTLE_MS = 8_000

# Tenant-specific selectors — adjust against a real snapshot (see module docstring).
SELECTORS = {
    # Container that wraps the absence calendar once it has finished loading.
    "ready": "[data-automation-id='calendar'], [data-automation-id='grid']",
    # One element per absence entry.
    "event": "[data-automation-id='calendarEvent'], [data-automation-id='gridRow']",
}


def _open_calendar(context: BrowserContext, cfg: Config) -> Page:
    page = context.new_page()
    page.goto(cfg.absence_calendar_url, wait_until="domcontentloaded")
    # Workday loads data after the initial HTML; wait for the view to settle.
    try:
        page.wait_for_selector(SELECTORS["ready"], timeout=RENDER_SETTLE_MS)
    except Exception:
        # Not fatal for `snapshot`; _parse_events will simply find nothing.
        page.wait_for_timeout(RENDER_SETTLE_MS)
    page.wait_for_load_state("networkidle")
    return page


def snapshot(context: BrowserContext, cfg: Config, out_dir: Path) -> Path:
    """Dump the rendered calendar page for selector development."""
    out_dir.mkdir(parents=True, exist_ok=True)
    page = _open_calendar(context, cfg)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    html_path = out_dir / f"absence-{stamp}.html"
    png_path = out_dir / f"absence-{stamp}.png"
    html_path.write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(png_path), full_page=True)
    return html_path


def _parse_events(page: Page, cfg: Config) -> list[AbsenceEvent]:
    """Extract absences from the rendered page.

    PLACEHOLDER parsing — replace the body once you've inspected a snapshot.
    The shape it must return is a list of AbsenceEvent (see models.py).
    """
    events: list[AbsenceEvent] = []
    for el in page.query_selector_all(SELECTORS["event"]):
        # Example of the kind of extraction you'll do once selectors are known:
        #
        #   person = el.get_attribute("data-worker-name")
        #   atype  = el.get_attribute("data-absence-type") or "Time off"
        #   start  = _parse_wd_date(el.get_attribute("data-start"))
        #   end    = _parse_wd_date(el.get_attribute("data-end"))
        #   events.append(AbsenceEvent(person, atype, start, end))
        #
        # Left unimplemented on purpose so a half-wired scraper fails loudly
        # instead of silently syncing garbage.
        raise NotImplementedError(
            "wdtools.workday._parse_events needs real selectors — run "
            "`python -m wdtools snapshot` and wire it up (see module docstring)."
        )
    return events


def scrape_absences(context: BrowserContext, cfg: Config) -> list[AbsenceEvent]:
    page = _open_calendar(context, cfg)
    return _parse_events(page, cfg)


def _parse_wd_date(raw: str) -> date:
    """Parse the date format Workday hands you. Adjust once known.

    Workday commonly uses ISO (YYYY-MM-DD) in data attributes and localized
    strings in visible text. Prefer data attributes.
    """
    return date.fromisoformat(raw[:10])
