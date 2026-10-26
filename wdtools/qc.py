"""Scrape Quarterly Connection reviews from the Workday "My Tasks" inbox.

Flow
----
The My Tasks (Unified Inbox) view lists tasks; a Quarterly Connection shows up as
``Quarterly Connection: <Employee>`` with an executor task "Complete Manager
Evaluation". The inbox's task list arrives as JSON (the ``utsxServe .../queryTasks``
response), so — like the absence calendar — we parse the *captured JSON* rather than
scrape DOM rows: it carries each task's title (→ employee), its effective/review
period (→ ``YEARQX``), and a direct URI to the evaluation task.

Opening that task shows an overview with the Review Period and a **Get Started**
button that leads into the guided "Complete Manager Evaluation": a wizard whose left
rail lists sections (Accomplishments, Priorities, … Review and Submit). Every section
except "Review and Submit" holds question(s) with the associate's answer.

Tenant note
-----------
Both the discovery (``queryTasks`` JSON) and the editor extraction (the task-wizard
left rail + read-only rich-text blocks) were wired against one real tenant's
capture. If the markup differs on another tenant, re-capture with
``wdtools qc-snapshot --task 1`` (it navigates overview → Get Started, dumps the
editor plus each frame, and follows the embedded form) and adjust ``SELECTORS``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

CACHE_VERSION = 1

from playwright.sync_api import BrowserContext, Page

from ._capture import NetworkCapture
from .config import Config
from .models import (
    PreviousPriorities,
    QCQuestion,
    QCSection,
    QuarterlyConnection,
    quarter_for,
)

logger = logging.getLogger(__name__)

RENDER_SETTLE_MS = 8_000
NAV_TIMEOUT_MS = 15_000

# --- Parsing the inbox task list JSON --------------------------------------
#
# The inbox list is served as {"queryResponseStringified": "<json>"} whose inner
# "data" is a list of unified-task entries. Each Quarterly Connection entry carries:
#   unifiedTaskMetadata.appliedSystemTags.TITLE.value          -> "Quarterly Connection: <Employee>"
#   unifiedTaskMetadata.appliedSystemTags.EFFECTIVE_DATE.value -> "YYYY-MM-DD" (period end)
#   unifiedTaskModel.taskExecutor.taskToExecute.uri            -> "/<tenant>/d/inst/…/rel-task/…​.htmld"

QC_TITLE_RE = re.compile(r"Quarterly Connection:\s*(?P<name>.+)", re.I)
# The guided editor's left-rail section we never read (it's the submit step).
SKIP_SECTIONS = {"review and submit"}

# Selectors for the guided editor (post Get Started). Derived from a real tenant
# capture: the editor is a task wizard whose left rail is a vertical navigation, and
# each section renders its question(s) and the associate's answer(s) as read-only
# rich-text blocks (the manager's own answer box is a separate, empty rich-text box).
SELECTORS = {
    "get_started": "[data-automation-id='wd-CommandButton']:has-text('Get Started'), "
    "button:has-text('Get Started')",
    # The task-wizard instance moniker (its label button navigates to the event view).
    "instance_label": "[data-automation-id='taskWizardInstanceLabel']",
    "section_nav_item": "[data-automation-id='verticalNavigationItem']",
    # Read-only rich text = question + associate answer (the manager's own answer box
    # is the editable ProseMirror, contenteditable='true', which we exclude here).
    "rich_text_readonly": "[data-automation-id='richTextContent'] "
    ".ProseMirror[contenteditable='false']",
}


@dataclass(frozen=True)
class _TaskRef:
    employee: str
    year: int
    quarter: int
    overview_uri: str
    title: str
    period_label: str


_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ],
        start=1,
    )
}


def period_to_quarter(text: str) -> tuple[int, int]:
    """Map a Workday review-period string to ``(year, quarter)``.

    Handles ``YYYY-MM-DD`` (the inbox effective date), ``MM/DD/YYYY - MM/DD/YYYY``
    (the overview's Review Period — the end date is used), ``Qn YYYY`` / ``YYYY Qn``,
    and ``<Month> YYYY``. Raises if none match (inspect the raw text, logged).
    """
    t = text.strip()
    m = re.search(r"(\d{4})-(\d{2})-\d{2}", t)  # ISO (effective date)
    if m:
        return int(m.group(1)), quarter_for(int(m.group(2)))
    us = re.findall(r"(\d{2})/(\d{2})/(\d{4})", t)  # MM/DD/YYYY, prefer the last (end)
    if us:
        mm, _dd, yyyy = us[-1]
        return int(yyyy), quarter_for(int(mm))
    m = re.search(r"Q([1-4]).{0,6}(20\d\d)", t, re.I)
    if m:
        return int(m.group(2)), int(m.group(1))
    m = re.search(r"(20\d\d).{0,6}Q([1-4])", t, re.I)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"([A-Za-z]+)\s+(20\d\d)", t)
    if m and m.group(1).lower() in _MONTHS:
        return int(m.group(2)), quarter_for(_MONTHS[m.group(1).lower()])
    raise ValueError(f"Could not derive a quarter from review period: {text!r}")


def _discover_tasks(capture: NetworkCapture) -> list[_TaskRef]:
    """Parse Quarterly Connection tasks from the captured inbox queryTasks JSON."""
    refs: list[_TaskRef] = []
    seen: set[str] = set()
    for payload in capture.json_payloads():
        if not isinstance(payload, dict) or "queryResponseStringified" not in payload:
            continue
        try:
            inner = json.loads(payload["queryResponseStringified"])
        except Exception:
            continue
        for entry in inner.get("data", []):
            tags = (
                entry.get("unifiedTaskMetadata", {}).get("appliedSystemTags", {}) or {}
            )
            title = (tags.get("TITLE") or {}).get("value", "") or ""
            m = QC_TITLE_RE.search(title)
            if not m:
                continue
            eff = (tags.get("EFFECTIVE_DATE") or {}).get("value", "") or ""
            tte = (
                entry.get("unifiedTaskModel", {})
                .get("taskExecutor", {})
                .get("taskToExecute", {})
                or {}
            )
            uri = tte.get("uri", "") or ""
            if not uri or uri in seen:
                continue
            seen.add(uri)
            try:
                year, quarter = period_to_quarter(eff)
            except ValueError as exc:
                logger.error("%s — skipping %s.", exc, title)
                continue
            refs.append(
                _TaskRef(
                    employee=m.group("name").strip(),
                    year=year,
                    quarter=quarter,
                    overview_uri=uri,
                    title=title,
                    period_label=eff,
                )
            )
    logger.info("Discovered %d Quarterly Connection task(s) from the inbox.", len(refs))
    return refs


# --- Browser navigation ----------------------------------------------------


def _base_url(cfg: Config) -> str:
    parts = urlsplit(cfg.my_tasks_url)
    return f"{parts.scheme}://{parts.netloc}"


def _abs(cfg: Config, uri: str) -> str:
    return uri if uri.startswith("http") else _base_url(cfg) + uri


def _to_htmld(uri: str) -> str:
    """Convert a mobile-style task uri to its desktop ``/d/…​.htmld`` form.

    e.g. ``/<tenant>/inst/1007$641183/rel-task/2998$40105`` ->
         ``/<tenant>/d/inst/1007$641183/rel-task/2998$40105.htmld``
    """
    if uri.endswith(".htmld"):
        return uri
    parts = uri.split("/")
    if len(parts) > 2 and parts[2] != "d":
        parts.insert(2, "d")
    return "/".join(parts) + ".htmld"


def _find_widget_uri(capture: NetworkCapture, match) -> str | None:
    """Return the ``uri`` of the most recent widget node for which ``match(node)`` is true."""
    found: str | None = None

    def walk(node) -> None:
        nonlocal found
        if isinstance(node, dict):
            if match(node) and node.get("uri"):
                found = node["uri"]
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for payload in capture.json_payloads():
        walk(payload)
    return found


def _find_get_started_uri(capture: NetworkCapture) -> str | None:
    """Scan captured payloads for the most recent 'Get Started' commandButton uri."""
    return _find_widget_uri(
        capture,
        lambda n: n.get("widget") == "commandButton"
        and (n.get("label") or "").strip().lower() == "get started",
    )


def _find_embedded_uri(capture: NetworkCapture) -> str | None:
    """The task-wizard embeddedEdit uri (the form holding the associate's answers)."""
    return _find_widget_uri(capture, lambda n: n.get("widget") == "embeddedEdit")


def _find_related_task_uri(capture: NetworkCapture, label: str) -> str | None:
    """URI of a related-task action (e.g. 'View Associate Reviews') from a rel-tasks menu."""
    want = label.strip().lower()
    return _find_widget_uri(
        capture,
        lambda n: n.get("widget") == "relatedTask"
        and (n.get("label") or "").strip().lower() == want,
    )


def _parse_grid_date(cell: dict) -> date | None:
    """A grid date cell -> date (handles MM/DD/YYYY ``text`` and a {Y,M,D} ``value``)."""
    t = cell.get("text")
    if isinstance(t, str):
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})", t)
        if m:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    val = cell.get("value")
    if isinstance(val, dict) and val.get("Y"):
        try:
            return date(int(val["Y"]), int(val["M"]), int(val["D"]))
        except (KeyError, ValueError, TypeError):
            return None
    return None


def _review_rows(capture: NetworkCapture) -> list[dict]:
    """Parse the Associate Reviews grid into rows {name, start, end, view_uri}.

    Cells carry ``label``s, so we key off those: the "Review" monikerList (name),
    the "Review Period Start/End Date" date cells, and the "View" commandButton
    (its uri is the review summary). Only "Quarterly Connection" rows are kept.
    """
    rows: list[dict] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if node.get("widget") == "row" and isinstance(node.get("cellsMap"), dict):
                name = None
                start = end = None
                extra_dates: list[date] = []
                view = None
                for cell in node["cellsMap"].values():
                    if not isinstance(cell, dict):
                        continue
                    label = (cell.get("label") or "").strip().lower()
                    widget = cell.get("widget")
                    if widget == "monikerList":
                        for ins in cell.get("instances") or []:
                            t = ins.get("text")
                            if isinstance(t, str) and t.startswith("Quarterly Connection"):
                                name = t
                    elif widget == "date":
                        d = _parse_grid_date(cell)
                        if d:
                            if "end" in label:
                                end = d
                            elif "start" in label:
                                start = d
                            else:
                                extra_dates.append(d)
                    elif widget == "commandButton" and label == "view":
                        view = cell.get("uri")
                if end is None and extra_dates:
                    end = max(extra_dates)
                if start is None and extra_dates:
                    start = min(extra_dates)
                if name and view and "/inst/" in view and "/rel-task/" in view:
                    rows.append(
                        {"name": name, "start": start, "end": end, "view_uri": view}
                    )
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for payload in capture.json_payloads():
        walk(payload)
    return rows


def _pick_previous_review(rows: list[dict], current_start: date) -> dict | None:
    """Most recent completed review whose end date is before ``current_start``."""
    cands = [r for r in rows if r.get("end") and r["end"] < current_start]
    return max(cands, key=lambda r: r["end"]) if cands else None


def _quarter_start(year: int, quarter: int) -> date:
    return date(year, (quarter - 1) * 3 + 1, 1)


def _read_priorities_columns(page: Page) -> tuple[str, str] | None:
    """On a review summary page, return (manager_html, associate_html) for Priorities.

    The summary renders each section as read-only rich-text blocks in the order
    [Question, Manager answer, Associate answer]; we find the Priorities question and
    take the next two blocks' HTML.
    """
    blocks = [
        ((el.inner_text() or "").strip(), el.inner_html() or "")
        for el in page.query_selector_all(SELECTORS["rich_text_readonly"])
    ]
    for i, (text, _html) in enumerate(blocks):
        low = text.lower()
        if low.startswith("what") and "priorit" in low and i + 2 < len(blocks):
            return blocks[i + 1][1], blocks[i + 2][1]  # (manager, associate)
    return None


def _fetch_previous_priorities(
    page: Page, cfg: Config, ref: _TaskRef
) -> PreviousPriorities | None:
    """Navigate from the open editor to the associate's prior QC and read Priorities.

    Chain: instance moniker → event view → associate related actions (menu JSON) →
    View Associate Reviews (grid JSON) → previous review summary → Priorities columns.
    Best-effort; returns None if any hop fails and no previous review is found. Uses a
    fresh capture so one employee's grid/menu JSON can't leak into another's.
    """
    sub = NetworkCapture()
    sub.attach(page)
    try:
        page.locator(SELECTORS["instance_label"]).first.click(timeout=NAV_TIMEOUT_MS)
        _settle(page)
    except Exception:
        logger.warning("prev-priorities: could not open event view for %s", ref.employee)
        return None

    ra_sel = (
        "[data-automation-id='relatedIconContainer']"
        f"[aria-label='Related Actions {ref.employee}']"
    )
    try:
        page.locator(ra_sel).first.click(timeout=NAV_TIMEOUT_MS)
        page.wait_for_timeout(2000)
        _settle(page)
    except Exception:
        logger.warning("prev-priorities: could not open related actions for %s", ref.employee)
        return None

    var_uri = _find_related_task_uri(sub, "View Associate Reviews")
    if not var_uri:
        logger.warning("prev-priorities: no 'View Associate Reviews' for %s", ref.employee)
        return None
    try:
        page.goto(_abs(cfg, _to_htmld(var_uri)), wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        _settle(page)
    except Exception:
        logger.warning("prev-priorities: could not open associate reviews for %s", ref.employee)
        return None

    prev = _pick_previous_review(_review_rows(sub), _quarter_start(ref.year, ref.quarter))
    if not prev:
        logger.info("prev-priorities: no previous review for %s", ref.employee)
        return None

    pstart = prev.get("start") or prev.get("end")
    year, quarter = pstart.year, quarter_for(pstart.month)
    try:
        page.goto(_abs(cfg, _to_htmld(prev["view_uri"])), wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        _settle(page)
        page.wait_for_selector(SELECTORS["rich_text_readonly"], timeout=RENDER_SETTLE_MS)
    except Exception:
        logger.warning("prev-priorities: could not open previous summary for %s", ref.employee)
        return PreviousPriorities(year=year, quarter=quarter)

    cols = _read_priorities_columns(page)
    if cols is None:
        logger.warning("prev-priorities: Priorities columns not found for %s", ref.employee)
        return PreviousPriorities(year=year, quarter=quarter)
    logger.info("prev-priorities: captured %dQ%d for %s", year, quarter, ref.employee)
    return PreviousPriorities(
        year=year, quarter=quarter, manager_html=cols[0], associate_html=cols[1]
    )


def _settle(page: Page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=RENDER_SETTLE_MS)
    except Exception:
        pass
    page.wait_for_timeout(300)


def _is_query_tasks(response) -> bool:
    return "queryTasks" in response.url


def _open_inbox(
    context: BrowserContext, cfg: Config, capture: NetworkCapture | None = None
) -> Page:
    """Open the inbox and block until its task-list JSON (queryTasks) is served.

    The task list loads via an XHR after navigation; dumping/parsing before it
    arrives yields an empty inbox (seen as a flake). We wait for that response and,
    if it doesn't come (e.g. served from cache), reload once to force it.
    """
    page = context.new_page()
    if capture is not None:
        capture.attach(page)  # attach before navigation to catch early XHR
    logger.info("Opening My Tasks inbox: %s", cfg.my_tasks_url)
    for attempt in (1, 2):
        try:
            with page.expect_response(_is_query_tasks, timeout=NAV_TIMEOUT_MS):
                if attempt == 1:
                    page.goto(cfg.my_tasks_url, wait_until="domcontentloaded")
                else:
                    logger.info("Inbox task list not seen; reloading (attempt %d).", attempt)
                    page.reload(wait_until="domcontentloaded")
            _settle(page)
            logger.info("My Tasks inbox loaded (task list received).")
            return page
        except Exception:
            logger.debug("queryTasks response not seen on attempt %d.", attempt)
            _settle(page)
    logger.warning("Inbox task list (queryTasks) never arrived; discovery may be empty.")
    return page


def _click_get_started(page: Page, cfg: Config, capture: NetworkCapture | None) -> bool:
    """Enter the guided editor: click the Get Started button, else navigate its uri."""
    try:
        page.locator(SELECTORS["get_started"]).first.click(timeout=NAV_TIMEOUT_MS)
        _settle(page)
        return True
    except Exception:
        logger.debug("Get Started DOM click failed; trying the parsed uri.")
    if capture is not None:
        gs = _find_get_started_uri(capture)
        if gs:
            logger.info("Navigating to Get Started uri: %s", gs)
            try:
                page.goto(_abs(cfg, _to_htmld(gs)), wait_until="domcontentloaded")
                _settle(page)
                return True
            except Exception:
                logger.warning("Could not navigate the Get Started uri %s", gs)
    return False


def _open_evaluation(
    page: Page, cfg: Config, ref: _TaskRef, capture: NetworkCapture | None = None
) -> bool:
    """Navigate to a task's overview and enter the guided editor. False on failure."""
    url = _abs(cfg, ref.overview_uri)
    logger.info("Opening evaluation for %s: %s", ref.employee, url)
    try:
        page.goto(url, wait_until="domcontentloaded")
    except Exception:
        logger.warning("Could not open overview for %s", ref.employee)
        return False
    _settle(page)
    if not _click_get_started(page, cfg, capture):
        logger.warning("Could not enter the guided editor for %s", ref.employee)
        return False
    return True


# --- Cache (XDG state) ------------------------------------------------------
#
# An associate's answers don't change once submitted, so we cache each scraped
# connection (keyed by its task overview uri) in the state dir. Later runs reuse
# cached tasks and only scrape new ones (--refresh re-scrapes everything; --offline
# skips Workday and works purely from the cache).


def _qc_to_dict(qc: QuarterlyConnection) -> dict:
    d = {
        "employee": qc.employee,
        "year": qc.year,
        "quarter": qc.quarter,
        "period_label": qc.period_label,
        "sections": [
            {
                "name": s.name,
                "questions": [
                    {"question": q.question, "answer": q.answer, "answer_html": q.answer_html}
                    for q in s.questions
                ],
            }
            for s in qc.sections
        ],
    }
    if qc.previous is not None:
        d["previous"] = {
            "year": qc.previous.year,
            "quarter": qc.previous.quarter,
            "manager_html": qc.previous.manager_html,
            "associate_html": qc.previous.associate_html,
        }
    return d


def _qc_from_dict(d: dict) -> QuarterlyConnection:
    prev = d.get("previous")
    previous = (
        PreviousPriorities(
            year=int(prev["year"]),
            quarter=int(prev["quarter"]),
            manager_html=prev.get("manager_html", ""),
            associate_html=prev.get("associate_html", ""),
        )
        if prev
        else None
    )
    return QuarterlyConnection(
        employee=d["employee"],
        year=int(d["year"]),
        quarter=int(d["quarter"]),
        period_label=d.get("period_label", ""),
        previous=previous,
        sections=tuple(
            QCSection(
                name=s["name"],
                questions=tuple(
                    QCQuestion(
                        question=q.get("question", ""),
                        answer=q.get("answer", ""),
                        answer_html=q.get("answer_html", ""),
                    )
                    for q in s.get("questions", [])
                ),
            )
            for s in d.get("sections", [])
        ),
    )


def _load_cache(cfg: Config) -> dict[str, dict]:
    """Return {overview_uri: qc_dict} from the cache file (empty if absent/invalid)."""
    path = Path(cfg.qc_cache_path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Ignoring unreadable QC cache at %s", path)
        return {}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return {}
    tasks = data.get("tasks", {})
    return tasks if isinstance(tasks, dict) else {}


def _save_cache(cfg: Config, tasks: dict[str, dict]) -> None:
    path = Path(cfg.qc_cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": CACHE_VERSION, "tasks": tasks}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.debug("Wrote %d cached connection(s) to %s", len(tasks), path)


def load_offline_connections(cfg: Config) -> list[QuarterlyConnection]:
    """All cached connections, for --offline runs (no Workday contact)."""
    cfg.validate_talent()
    tasks = _load_cache(cfg)
    return [_qc_from_dict(v) for v in tasks.values()]


# --- Reading one evaluation's sections --------------------------------------


def _clean(text: str) -> str:
    """Normalize a nav label (the rail appends an overflow ellipsis)."""
    return re.sub(r"[\s.…]+$", "", (text or "").strip()).strip()


def _section_names(page: Page) -> list[str]:
    names: list[str] = []
    try:
        for el in page.query_selector_all(SELECTORS["section_nav_item"]):
            txt = _clean(el.inner_text())
            if txt:
                names.append(txt)
    except Exception:
        logger.debug("Failed to read section nav with %s", SELECTORS["section_nav_item"])
    return names


def _read_questions(page: Page) -> list[QCQuestion]:
    """Pair the section's read-only rich-text blocks as (question, associate answer).

    The read-only blocks render in order [question, answer, question, answer, …]
    (the manager's own editable answer box is excluded by the selector). We keep both
    the plain text (for console) and the HTML (for formatted Doc output).
    """
    blocks = [
        ((el.inner_text() or "").strip(), el.inner_html() or "")
        for el in page.query_selector_all(SELECTORS["rich_text_readonly"])
    ]
    out: list[QCQuestion] = []
    for i in range(0, len(blocks), 2):
        q_text = blocks[i][0]
        a_text, a_html = blocks[i + 1] if i + 1 < len(blocks) else ("", "")
        out.append(QCQuestion(question=q_text, answer=a_text, answer_html=a_html))
    return out


def _read_section(page: Page, name: str) -> QCSection:
    try:
        page.locator(SELECTORS["section_nav_item"]).filter(
            has_text=name
        ).first.click(timeout=NAV_TIMEOUT_MS)
        _settle(page)
        page.wait_for_timeout(800)  # let the section's rich text swap in
        page.wait_for_selector(SELECTORS["rich_text_readonly"], timeout=RENDER_SETTLE_MS)
    except Exception:
        logger.warning("Could not open section %r", name)
    questions = _read_questions(page)
    logger.info("Section %r: %d question(s).", name, len(questions))
    return QCSection(name=name, questions=tuple(questions))


def scrape_quarterly_connections(
    context: BrowserContext, cfg: Config, refresh: bool = False
) -> list[QuarterlyConnection]:
    """Discover tasks from the inbox and return each employee's connection.

    Cached tasks (keyed by overview uri in the state dir) are reused; only tasks not
    yet cached are scraped from the wizard. ``refresh=True`` re-scrapes every task and
    rewrites the cache.
    """
    cfg.validate_talent()
    capture = NetworkCapture()
    page = _open_inbox(context, cfg, capture=capture)
    refs = _discover_tasks(capture)
    if not refs:
        logger.warning(
            "No 'Quarterly Connection' tasks found in the inbox JSON. If you expected "
            "some, re-run `wdtools qc-snapshot` and check the queryTasks payload."
        )

    cache = _load_cache(cfg)
    connections: list[QuarterlyConnection] = []
    scraped = 0
    for ref in refs:
        key = ref.overview_uri
        if not refresh and key in cache:
            logger.info("Using cached connection for %s (%dQ%d)",
                        ref.employee, ref.year, ref.quarter)
            connections.append(_qc_from_dict(cache[key]))
            continue

        logger.info("Processing %s (%dQ%d)", ref.employee, ref.year, ref.quarter)
        if not _open_evaluation(page, cfg, ref, capture):
            continue
        # Let the task wizard finish rendering its first section + left rail.
        try:
            page.wait_for_selector(SELECTORS["section_nav_item"], timeout=RENDER_SETTLE_MS)
            page.wait_for_timeout(1500)
        except Exception:
            logger.warning("Guided editor nav did not render for %s", ref.employee)
        sections = [
            _read_section(page, name)
            for name in _section_names(page)
            if name.strip().lower() not in SKIP_SECTIONS
        ]
        previous = _fetch_previous_priorities(page, cfg, ref)
        qc = QuarterlyConnection(
            employee=ref.employee,
            year=ref.year,
            quarter=ref.quarter,
            sections=tuple(sections),
            period_label=ref.period_label,
            previous=previous,
        )
        connections.append(qc)
        cache[key] = _qc_to_dict(qc)
        scraped += 1

    if scraped:
        _save_cache(cfg, cache)
    logger.info("Parsed %d quarterly connection(s) (%d scraped, %d from cache).",
                len(connections), scraped, len(connections) - scraped)
    return connections


# --- Snapshot (selector development) ---------------------------------------


def snapshot(
    context: BrowserContext,
    cfg: Config,
    out_dir: Path,
    task_index: int | None = None,
) -> Path:
    """Dump the inbox (and optionally one task's editor pages) for selector dev.

    Writes HTML + screenshots + captured XHR/JSON. With ``task_index`` (1-based) it
    navigates that task's overview and clicks Get Started, then snapshots the editor
    and each non-"Review and Submit" section so the question/answer selectors and the
    editor JSON can be derived.
    """
    cfg.validate_talent()
    out_dir.mkdir(parents=True, exist_ok=True)
    # Include document loads too: the task-wizard's embeddedEdit (which holds the
    # associate's answers) loads as a document navigation, not an xhr/fetch.
    capture = NetworkCapture(resource_types=("xhr", "fetch", "document"))
    page = _open_inbox(context, cfg, capture=capture)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    def _dump(label: str) -> None:
        (out_dir / f"qc-{stamp}-{label}.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(out_dir / f"qc-{stamp}-{label}.png"), full_page=True)
        # The answers render inside embedded frames; dump each frame's HTML too.
        for fi, frame in enumerate(page.frames):
            if frame == page.main_frame:
                continue
            try:
                html = frame.content()
            except Exception:
                continue
            path = out_dir / f"qc-{stamp}-{label}-frame{fi:02d}.html"
            path.write_text(f"<!-- frame url: {frame.url} -->\n{html}", encoding="utf-8")

    _dump("inbox")
    refs = _discover_tasks(capture)
    for i, r in enumerate(refs, start=1):
        logger.info("  task %d: %s (%dQ%d) -> %s", i, r.employee, r.year, r.quarter, r.overview_uri)

    if task_index is not None:
        if 1 <= task_index <= len(refs):
            ref = refs[task_index - 1]
            logger.info("Snapshotting task %d: %s", task_index, ref.employee)
            page.goto(_abs(cfg, ref.overview_uri), wait_until="domcontentloaded")
            _settle(page)
            _dump("task-overview")
            if _click_get_started(page, cfg, capture):
                # Give the embedded form time to load inside the wizard.
                page.wait_for_timeout(4000)
                _dump("editor")

                # Event view first, while we're still on the editor (clicking the
                # task-wizard instance moniker; a derived inst URL 404s). The
                # embedded-form navigation below leaves the editor, so do this first.
                logger.info("Clicking the instance moniker to open the event view.")
                try:
                    page.locator(SELECTORS["instance_label"]).first.click(timeout=NAV_TIMEOUT_MS)
                    page.wait_for_timeout(3000)
                    _settle(page)
                    _dump("event-view")
                except Exception:
                    logger.warning("Could not open the event view via the instance moniker.")

                # Associate's related-actions menu (the "For" row ellipsis) — its menu
                # should list Talent → View Associate Reviews. Capture it so we can map
                # that hop.
                ra_sel = (
                    "[data-automation-id='relatedIconContainer']"
                    f"[aria-label='Related Actions {ref.employee}']"
                )
                logger.info("Opening associate related actions: %s", ref.employee)
                try:
                    page.locator(ra_sel).first.click(timeout=NAV_TIMEOUT_MS)
                    page.wait_for_timeout(2500)
                    _settle(page)
                    _dump("assoc-related-actions")
                except Exception:
                    logger.warning("Could not open associate related actions for %s",
                                   ref.employee)

                # View Associate Reviews — its uri comes from the related-actions menu
                # JSON (a relatedTask). Navigate there and dump the Completed table.
                var_uri = _find_related_task_uri(capture, "View Associate Reviews")
                if var_uri:
                    logger.info("Opening View Associate Reviews: %s", var_uri)
                    try:
                        page.goto(_abs(cfg, _to_htmld(var_uri)), wait_until="domcontentloaded")
                        page.wait_for_timeout(3000)
                        _settle(page)
                        _dump("associate-reviews")
                    except Exception:
                        logger.warning("Could not open View Associate Reviews %s", var_uri)

                    # Previous QC summary — pick the most recent completed review before
                    # this task's quarter and open its View uri (holds the Priorities).
                    rows = _review_rows(capture)
                    prev = _pick_previous_review(rows, _quarter_start(ref.year, ref.quarter))
                    logger.info("Review rows: %d; previous: %s", len(rows),
                                (prev or {}).get("view_uri"))
                    if prev:
                        try:
                            page.goto(_abs(cfg, _to_htmld(prev["view_uri"])),
                                      wait_until="domcontentloaded")
                            page.wait_for_timeout(3000)
                            _settle(page)
                            _dump("previous-review")
                        except Exception:
                            logger.warning("Could not open previous review %s",
                                           prev["view_uri"])
                else:
                    logger.warning("No 'View Associate Reviews' action found in the menu.")

                # Follow the embeddedEdit uri directly so its form (the associate's
                # answers) is captured even if it didn't render inline. Done last
                # because it navigates away from the editor.
                emb = _find_embedded_uri(capture)
                if emb:
                    logger.info("Opening embeddedEdit directly: %s", emb)
                    try:
                        page.goto(_abs(cfg, emb), wait_until="domcontentloaded")
                        page.wait_for_timeout(3000)
                        _settle(page)
                        _dump("embedded")
                    except Exception:
                        logger.warning("Could not open embeddedEdit uri %s", emb)
            else:
                logger.warning("Could not enter the guided editor for the snapshot.")
        else:
            logger.warning("task-index %d out of range (found %d task(s)).",
                           task_index, len(refs))

    index_path = capture.dump(out_dir, stamp, prefix="qc")
    logger.info("Snapshot written to %s", out_dir)
    return index_path


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", text.strip())[:40] or "section"
