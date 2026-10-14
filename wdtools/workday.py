"""Scrape the team absence calendar from Workday.

How it works
------------
The Team Absence Calendar is a prompted report: opening its URL shows a
parameter dialog (Organizations / Workers), and clicking OK runs it. The grid is
then driven by a "flowController" JSON response whose calendar widget holds a
flat list of per-day ``calendarEntry`` widgets — each carrying the worker, the
date, the entry type (time off vs. holiday), a ``blockIdentifier`` that groups a
request's days and encodes its status, and Workday's own duration label.

``scrape_absences`` drives that flow: it opens the calendar, dismisses the
prompt, pages across the sync window a month at a time (one view = one month),
and folds the captured entries into :class:`AbsenceEvent`\\ s — approved time off
and public holidays (row placeholders dropped), each contiguous run of days its
own event.

Tenant note
-----------
The selectors and JSON shape below were wired up against one Workday tenant. If
the markup differs on yours, re-run ``wdtools snapshot`` — it dumps the rendered
HTML, a screenshot, and every XHR/fetch JSON response (with a ``*-xhr-index.json``
manifest) so you can re-check them. If your tenant offers an iCal / "Export to
Calendar" feed on this view, that is far more stable than scraping — see README.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, Response

from .config import Config
from .models import AbsenceEvent

logger = logging.getLogger(__name__)

# Give the SPA time to finish its async render after navigation.
RENDER_SETTLE_MS = 8_000
# How long to wait for the parameter prompt dialog to appear before giving up on
# it (it may not appear at all if Workday remembers the last run).
PROMPT_TIMEOUT_MS = 15_000
# Safety cap on how many times we click a paging button in either direction.
MAX_PAGING_STEPS = 40

# Selectors for the Team Absence Calendar (Workday "nyw:Calendar_Container").
SELECTORS = {
    # The "OK" button on the parameter prompt the calendar opens with. Clicking it
    # runs the report and renders the grid.
    "prompt_ok": "[data-automation-id='wd-CommandButton_uic_okButton']",
    # Calendar chrome; present once the view has rendered.
    "ready": "[data-automation-id='calendarToolbar']",
    # The period heading (e.g. "October 2026" in Month view); used to detect paging.
    "period_title": "[data-automation-id='dateRangeTitle']",
    # Interval selector: the button that opens Day/Week/Month/Year, and the
    # "Month" option inside the menu it opens.
    "view_label": "[data-automation-id='calendarViewSelection'] [data-automation-id='label']",
    "view_month_option": "[data-automation-id='dropdown-option'][data-automation-label='Month']",
    # Paging buttons (named *Month* but their step follows the active interval).
    "next_month": "[data-automation-id='nextMonthButton']",
    "prev_month": "[data-automation-id='prevMonthButton']",
}

# The calendar serves two entry types: personal time off and public holidays.
TIME_OFF_TYPE_PREFIX = "Team Time Off"
HOLIDAY_TYPE_PREFIX = "Holiday"
# Generic, privacy-preserving labels — Workday's own team view doesn't reveal the
# absence type (vacation vs sick), so neither do we.
OUT_OF_OFFICE_LABEL = "Out of office"
PUBLIC_HOLIDAY_LABEL = "Public holiday"
# Only approved time-off requests are synced (holidays have no status).
APPROVED_STATUS = "approved"
# blockIdentifier is "<plan name><32-hex request id><status>", e.g.
# "ITA Sick Time Off70905ad3d0441000a79612d3c2130000Approved". Plan names end in
# "...Time Off", whose trailing "ff" are also hex, so we anchor on the Title-Case
# status word (the hash is lowercase hex) to pin the 32-char request id correctly.
_BLOCK_RE = re.compile(r"^.*?[0-9a-f]{32}(?P<status>[A-Z][A-Za-z ]*)$")


def _run_prompt(page: Page) -> None:
    """Dismiss the parameter prompt the calendar opens with by clicking OK.

    The Team Absence Calendar is a prompted report: it first shows a dialog
    (Organizations / Workers / Include Self) and only renders the grid once you
    click OK. We accept whatever defaults Workday pre-fills. If no prompt shows
    up (already configured, or a different view), we just carry on.
    """
    try:
        ok = page.wait_for_selector(
            SELECTORS["prompt_ok"], timeout=PROMPT_TIMEOUT_MS, state="visible"
        )
    except Exception:
        logger.debug("No parameter prompt appeared; continuing.")
        return
    if ok is not None:
        logger.info("Dismissing calendar parameter prompt (clicking OK).")
        ok.click()


class _NetworkCapture:
    """Collect XHR/fetch responses seen while the calendar loads.

    We only hold lightweight request metadata plus the live ``Response`` object
    during load; bodies are read later (``dump``), while we're still parked on the
    calendar page so Playwright can still serve them.
    """

    def __init__(self) -> None:
        self._entries: list[dict] = []

    def attach(self, page: Page) -> None:
        page.on("response", self._on_response)

    def _on_response(self, response: Response) -> None:
        # This runs inside Playwright's event loop; an exception here poisons the
        # connection and breaks later page calls, so never let one escape.
        try:
            req = response.request
            if req.resource_type not in ("xhr", "fetch"):
                return
            self._entries.append(
                {
                    "url": response.url,
                    "status": response.status,
                    "method": req.method,
                    "resource_type": req.resource_type,
                    "content_type": (response.headers or {}).get("content-type", ""),
                    "post_data": _safe_post_data(req),
                    "_response": response,
                }
            )
        except Exception:
            pass

    def json_payloads(self) -> Iterator[dict]:
        """Parsed JSON bodies of every captured response (unparseable ones skipped).

        Entry extraction scans all of them — month navigation loads its data
        through an endpoint we don't try to name, so filtering by URL could drop it.
        """
        for entry in self._entries:
            response = entry.get("_response")
            if response is None:
                continue
            try:
                yield json.loads(response.text())
            except Exception:
                continue

    def response_summary(self) -> str:
        """Short description of what was captured, for diagnostics on failure."""
        total = len(self._entries)
        cal = sum(1 for e in self._entries if _is_calendar_url(e.get("url", "")))
        return f"{total} XHR/fetch response(s) captured, {cal} from calendar URLs"

    def dump(self, out_dir: Path, stamp: str) -> Path:
        """Write captured JSON responses + an index. Returns the index path."""
        index: list[dict] = []
        for i, entry in enumerate(self._entries):
            response: Response = entry.pop("_response")
            ctype = entry["content_type"].lower()
            # Keep anything that looks like structured data; Workday often serves
            # JSON without a clean content-type, so sniff the body too.
            try:
                body = response.text()
            except Exception:
                body = None
            looks_json = "json" in ctype or (
                body is not None and body.lstrip()[:1] in ("{", "[")
            )
            if not body or not looks_json:
                continue
            fname = f"absence-{stamp}-xhr-{i:03d}-{_slug(entry['url'])}.json"
            (out_dir / fname).write_text(body, encoding="utf-8")
            index.append({**entry, "bytes": len(body), "file": fname})

        index_path = out_dir / f"absence-{stamp}-xhr-index.json"
        index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
        return index_path


def _safe_post_data(request) -> str | None:
    """Request body as text, without Playwright's utf-8-only ``post_data``.

    Workday POSTs binary bodies; ``request.post_data`` decodes base64 as strict
    utf-8 and raises on them. Read the raw buffer and decode leniently instead.
    """
    try:
        raw = request.post_data_buffer
    except Exception:
        return None
    if not raw:
        return None
    return raw.decode("utf-8", "replace")


def _slug(url: str) -> str:
    """A short, filesystem-safe hint derived from a URL's path tail."""
    path = re.sub(r"https?://[^/]+", "", url).split("?", 1)[0]
    tail = "-".join(p for p in path.split("/") if p)[-60:]
    return re.sub(r"[^A-Za-z0-9._-]", "_", tail) or "root"


def _open_calendar(
    context: BrowserContext, cfg: Config, capture: _NetworkCapture | None = None
) -> Page:
    page = context.new_page()
    if capture is not None:
        capture.attach(page)  # must be attached before navigation to catch early XHR
    logger.info("Opening absence calendar: %s", cfg.absence_calendar_url)
    page.goto(cfg.absence_calendar_url, wait_until="domcontentloaded")
    # The calendar opens on a parameter prompt; click OK and block until the grid's
    # own data response actually arrives — relying on a selector/networkidle beat
    # instead races the OK click and sometimes leaves us with no calendar data.
    try:
        with page.expect_response(_is_calendar_response, timeout=PROMPT_TIMEOUT_MS):
            _run_prompt(page)
    except Exception:
        # No prompt and/or no calendar response seen (e.g. a remembered run); fall
        # back to a plain settle so `snapshot` still captures whatever rendered.
        try:
            page.wait_for_selector(SELECTORS["ready"], timeout=RENDER_SETTLE_MS)
        except Exception:
            page.wait_for_timeout(RENDER_SETTLE_MS)
    try:
        page.wait_for_load_state("networkidle", timeout=RENDER_SETTLE_MS)
    except Exception:
        pass
    logger.info("Calendar view loaded.")
    # Month view makes paging cross a month per click (fewer, reliable loads).
    _select_month_view(page)
    return page


def snapshot(
    context: BrowserContext,
    cfg: Config,
    out_dir: Path,
    open_view_menu: bool = False,
) -> Path:
    """Dump the rendered calendar page + its XHR data for selector development.

    With ``open_view_menu`` we first click the interval selector so the
    Day/Week/Month/Year menu is captured in the HTML — its options aren't in the
    DOM until it's opened.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    capture = _NetworkCapture()
    page = _open_calendar(context, cfg, capture=capture)
    if open_view_menu:
        _open_view_menu(page)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    html_path = out_dir / f"absence-{stamp}.html"
    png_path = out_dir / f"absence-{stamp}.png"
    html_path.write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(png_path), full_page=True)
    capture.dump(out_dir, stamp)
    return html_path


def _select_month_view(page: Page) -> None:
    """Switch the calendar to its Month interval so each page is a whole month.

    The default headless interval is weekly, where adjacent weeks are already in
    the preloaded month — so "next" fires no request and coverage never advances.
    In Month view each "next" crosses a month boundary and loads fresh data.
    Best-effort: if anything is missing we leave the view as-is.
    """
    try:
        label = page.wait_for_selector(
            SELECTORS["view_label"], timeout=RENDER_SETTLE_MS, state="visible"
        )
    except Exception:
        label = None
    if label is None:
        logger.debug("Interval selector not found; keeping the default view.")
        return
    if (label.get_attribute("title") or "").strip().lower() == "month":
        logger.debug("Calendar already in Month view.")
        return

    logger.info("Switching calendar to Month view.")
    label.click()
    try:
        option = page.wait_for_selector(
            SELECTORS["view_month_option"], timeout=RENDER_SETTLE_MS, state="visible"
        )
    except Exception:
        option = None
    if option is None:
        logger.warning("Month option not found; staying in the default view.")
        return
    option.click()
    try:
        page.wait_for_load_state("networkidle", timeout=RENDER_SETTLE_MS)
    except Exception:
        pass
    page.wait_for_timeout(300)
    logger.info("Calendar switched to Month view (now showing %s).", _period_label(page))


def _open_view_menu(page: Page) -> None:
    """Click the interval selector to render its menu (for snapshot inspection)."""
    for selector in (
        "[data-automation-id='calendarViewSelection'] [data-automation-id='label']",
        "[data-automation-id='calendarViewSelection']",
    ):
        try:
            el = page.wait_for_selector(selector, timeout=RENDER_SETTLE_MS, state="visible")
        except Exception:
            el = None
        if el is not None:
            el.click()
            page.wait_for_timeout(1000)
            logger.info("Opened interval selector menu via %s", selector)
            return
    logger.warning("Could not find the interval selector to open its menu.")


# --- Parsing the calendar JSON --------------------------------------------
#
# The grid is driven by a "flowController" (and, while paging, "calendarchunking")
# JSON response. Its calendar widget holds a flat list of per-day "calendarEntry"
# widgets. We recurse the payload to find them wherever they live, then fold the
# days of each approved time-off request into contiguous AbsenceEvents.


def _iter_calendar_entries(node) -> Iterator[dict]:
    """Yield every ``calendarEntry`` widget anywhere in a payload."""
    if isinstance(node, dict):
        if node.get("widget") == "calendarEntry":
            yield node
        for value in node.values():
            yield from _iter_calendar_entries(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_calendar_entries(value)


def _entry_type(entry: dict) -> str | None:
    try:
        return entry["type"]["instances"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return None


def _entry_owner(entry: dict) -> tuple[str | None, str | None]:
    """Return (display name, stable instance id) of the entry's worker."""
    try:
        inst = entry["owner"]["header"]["instances"][0]
        return inst.get("text"), inst.get("instanceId")
    except (KeyError, IndexError, TypeError):
        return None, None


def _entry_date(entry: dict) -> date | None:
    value = (entry.get("date") or {}).get("value") or {}
    try:
        return date(int(value["Y"]), int(value["M"]), int(value["D"]))
    except (KeyError, ValueError, TypeError):
        return None


def _entry_block(entry: dict) -> str | None:
    return (entry.get("blockIdentifier") or {}).get("value")


def _block_status(block: str) -> str | None:
    match = _BLOCK_RE.match(block)
    return match.group("status").strip() if match else None


def _contiguous_runs(dates: list[date]) -> list[list[date]]:
    """Split sorted dates into runs of consecutive calendar days.

    Workday draws one block per contiguous run (so an absence that skips a
    weekend shows as two blocks); mirroring that keeps us from marking a gap day
    as absence.
    """
    runs: list[list[date]] = []
    current: list[date] = []
    for day in dates:
        if current and (day - current[-1]).days == 1:
            current.append(day)
        else:
            if current:
                runs.append(current)
            current = [day]
    if current:
        runs.append(current)
    return runs


def _classify(entry: dict) -> tuple[str, str] | None:
    """Return ``(group_discriminator, label)`` for an entry we sync, else None.

    Time off is grouped by its ``blockIdentifier`` (one per request, so distinct
    requests never merge) and kept only when approved. Holidays carry no block or
    status, so all of a worker's holidays share one discriminator and are split
    into events purely by contiguous dates.
    """
    etype = _entry_type(entry) or ""
    if etype.startswith(TIME_OFF_TYPE_PREFIX):
        block = _entry_block(entry)
        if not block:
            return None  # row placeholder (style "hidden"), carries no absence
        if (_block_status(block) or "").lower() != APPROVED_STATUS:
            return None
        return block, OUT_OF_OFFICE_LABEL
    if etype.startswith(HOLIDAY_TYPE_PREFIX):
        return "holiday", PUBLIC_HOLIDAY_LABEL
    return None


def _events_from_entries(entries: Iterator[dict]) -> list[AbsenceEvent]:
    """Fold per-day calendar entries into AbsenceEvents.

    A set of dates per group dedups the boundary days shared between adjacent
    months; each contiguous run of days becomes one event.
    """
    groups: dict[tuple, dict] = defaultdict(
        lambda: {"dates": set(), "person": None, "person_id": None, "label": ""}
    )
    for entry in entries:
        classified = _classify(entry)
        if classified is None:
            continue
        discriminator, label = classified
        day = _entry_date(entry)
        if day is None:
            continue
        person, person_id = _entry_owner(entry)
        group = groups[(person_id or person, discriminator)]
        group["dates"].add(day)
        group["label"] = label
        if person:
            group["person"] = person
        if person_id:
            group["person_id"] = person_id

    events: list[AbsenceEvent] = []
    for group in groups.values():
        if not group["person"]:
            continue
        for run in _contiguous_runs(sorted(group["dates"])):
            events.append(
                AbsenceEvent(
                    person=group["person"],
                    absence_type=group["label"],
                    start=run[0],
                    end=run[-1],
                    person_id=group["person_id"],
                )
            )
    return events


# --- Paging through the window --------------------------------------------
#
# Headless, the calendar opens in a 7-day interval; we switch it to Month view so
# each "next"/"prev" click is exactly one month, then page a deterministic number
# of months to cover the window, detecting each advance from the period heading.


def _is_calendar_url(url: str) -> bool:
    return "flowController" in url or "calendarchunking" in url


def _is_calendar_response(response: Response) -> bool:
    return _is_calendar_url(response.url)


def _month_span(start: date, end: date) -> int:
    """Whole calendar months from ``start`` to ``end`` (never negative)."""
    return max(0, (end.year - start.year) * 12 + (end.month - start.month))


def _period_label(page: Page) -> str:
    """The calendar's current period heading, e.g. 'October 2026' in Month view."""
    try:
        el = page.query_selector(SELECTORS["period_title"])
        return (el.inner_text().strip() if el else "") or ""
    except Exception:
        return ""


def _click_nav(page: Page, selector: str) -> bool:
    """Click a paging button and wait for the period heading to actually change.

    Advancement is detected from the on-screen heading rather than the network:
    month navigation loads its data through an endpoint we can't reliably name, so
    waiting on a specific response just times out. A changed heading means the new
    month rendered; we then settle on network idle to capture its entries.

    Returns False if the button is absent (nothing more to page).
    """
    before = _period_label(page)
    try:
        button = page.wait_for_selector(
            selector, state="visible", timeout=RENDER_SETTLE_MS
        )
    except Exception:
        return False
    if button is None:
        return False
    button.click()
    try:
        page.wait_for_function(
            "(prev) => { const el = document.querySelector("
            "\"[data-automation-id='dateRangeTitle']\");"
            " return el && el.textContent.trim() && el.textContent.trim() !== prev; }",
            arg=before,
            timeout=RENDER_SETTLE_MS,
        )
    except Exception:
        logger.debug("Period heading did not change after clicking %s", selector)
    try:
        page.wait_for_load_state("networkidle", timeout=RENDER_SETTLE_MS)
    except Exception:
        pass
    page.wait_for_timeout(200)
    logger.debug("Paged %s -> %s", before, _period_label(page))
    return True


def _page_through_window(page: Page, cfg: Config, capture: _NetworkCapture) -> None:
    """Page across the sync window a month at a time so every month is captured.

    In Month view each click is exactly one month, so we step deterministically:
    back to the earliest month in the window, then forward across the whole span.
    The network capture records each month's data as the headings advance.
    """
    today = date.today()
    window_start = today - timedelta(days=cfg.days_back)
    window_end = today + timedelta(days=cfg.days_forward)
    back = min(_month_span(window_start, today), MAX_PAGING_STEPS)
    forward = min(_month_span(today, window_end), MAX_PAGING_STEPS)
    logger.info(
        "Paging %s .. %s (%d month(s) back, %d forward)",
        window_start, window_end, back, forward,
    )

    for _ in range(back):
        if not _click_nav(page, SELECTORS["prev_month"]):
            break
    for _ in range(back + forward):
        if not _click_nav(page, SELECTORS["next_month"]):
            break


def scrape_absences(context: BrowserContext, cfg: Config) -> list[AbsenceEvent]:
    capture = _NetworkCapture()
    page = _open_calendar(context, cfg, capture=capture)
    _page_through_window(page, cfg, capture)

    # Scan every captured payload (not just calendar URLs) so a change in which
    # endpoint serves the grid can't silently drop data.
    entries: list[dict] = []
    for payload in capture.json_payloads():
        entries.extend(_iter_calendar_entries(payload))
    logger.info("Found %d calendar entries across captured payloads.", len(entries))
    if not entries:
        raise RuntimeError(
            "No calendar entries found — the absence calendar didn't load as "
            f"expected ({capture.response_summary()}). Re-run `wdtools snapshot` "
            "and inspect the output."
        )
    events = _events_from_entries(entries)
    logger.info("Parsed %d absence event(s) (approved time off + holidays).", len(events))
    return events
