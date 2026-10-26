"""Write Quarterly Connections into a Google Doc via the official Docs API.

Each employee's connection is appended into a per-quarter *tab* (e.g. "2026Q1 QC")
that must already exist in the target doc — the Docs API can read and write tab
content but cannot create tabs, so the tabs are created by hand once per quarter.

Safety: writing is idempotent at employee granularity. Before appending an
employee we check the tab for a Heading-1 paragraph with that name; if present we
*skip* it rather than re-insert or edit, so a manager's typed responses are never
clobbered and re-runs never duplicate.
"""

from __future__ import annotations

import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .config import Config
from .models import QuarterlyConnection
from .richtext import Para, Run, html_to_paras

logger = logging.getLogger(__name__)

# Write access to Google Docs. Kept in its own token file so it doesn't disturb
# the calendar sync's token (which holds only the calendar.events scope).
SCOPES = ["https://www.googleapis.com/auth/documents"]

# Title of the per-employee section where managers type their responses. It lives
# after the associate's content and is preserved across `--replace` re-imports.
MANAGER_SECTION_TITLE = "Manager Response"


def _credentials(cfg: Config) -> Credentials:
    creds: Credentials | None = None
    if Path(cfg.google_docs_token_path).exists():
        creds = Credentials.from_authorized_user_file(
            str(cfg.google_docs_token_path), SCOPES
        )
    if creds and creds.valid:
        logger.debug("Using cached Google Docs token at %s", cfg.google_docs_token_path)
        return creds
    if creds and creds.expired and creds.refresh_token:
        logger.info("Refreshing expired Google Docs token.")
        creds.refresh(Request())
    else:
        if not Path(cfg.google_credentials_path).exists():
            raise FileNotFoundError(
                f"Missing Google OAuth client at {cfg.google_credentials_path}. "
                "Run `wdtools setup-google` first."
            )
        logger.info("No valid Docs token; starting Google consent flow in a browser.")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(cfg.google_credentials_path), SCOPES
        )
        creds = flow.run_local_server(port=0)
    Path(cfg.google_docs_token_path).write_text(creds.to_json(), encoding="utf-8")
    return creds


def _service(cfg: Config):
    return build("docs", "v1", credentials=_credentials(cfg), cache_discovery=False)


def authorize(cfg: Config):
    """Run the Docs consent flow (writing docs_token.json) and return a service."""
    return _service(cfg)


# --- Tab discovery ---------------------------------------------------------


def _iter_tabs(tabs: list[dict]):
    """Yield every tab (including nested child tabs) in document order."""
    for tab in tabs or []:
        yield tab
        yield from _iter_tabs(tab.get("childTabs", []))


def _tab_title(tab: dict) -> str:
    return (tab.get("tabProperties", {}) or {}).get("title", "")


def find_tab(doc: dict, tab_name: str) -> dict | None:
    """Return the tab whose title matches ``tab_name`` (case-insensitive), or None."""
    want = tab_name.strip().lower()
    for tab in _iter_tabs(doc.get("tabs", [])):
        if _tab_title(tab).strip().lower() == want:
            return tab
    return None


def _available_tab_names(doc: dict) -> list[str]:
    return [_tab_title(t) for t in _iter_tabs(doc.get("tabs", []))]


# --- Reading existing content ----------------------------------------------


def _paragraph_text(element: dict) -> str:
    """Concatenated text of a structural element that is a paragraph (else "")."""
    para = element.get("paragraph")
    if not para:
        return ""
    out = []
    for el in para.get("elements", []):
        run = el.get("textRun")
        if run:
            out.append(run.get("content", ""))
    return "".join(out)


def _employee_already_present(tab: dict, employee: str) -> bool:
    """True if a Heading-1 paragraph in the tab already names this employee."""
    want = employee.strip().lower()
    body = (tab.get("documentTab", {}) or {}).get("body", {}) or {}
    for element in body.get("content", []):
        para = element.get("paragraph")
        if not para:
            continue
        style = (para.get("paragraphStyle", {}) or {}).get("namedStyleType")
        if style == "HEADING_1" and _paragraph_text(element).strip().lower() == want:
            return True
    return False


def _body_end_index(tab: dict) -> int:
    """The end index of the tab body (one past its final, undeletable newline)."""
    body = (tab.get("documentTab", {}) or {}).get("body", {}) or {}
    content = body.get("content", [])
    if not content:
        return 1
    return content[-1].get("endIndex", 1)


def _heading1_elements(tab: dict) -> list[dict]:
    """All Heading-1 structural elements in the tab body, in order."""
    body = (tab.get("documentTab", {}) or {}).get("body", {}) or {}
    out = []
    for el in body.get("content", []):
        para = el.get("paragraph")
        if para and (para.get("paragraphStyle", {}) or {}).get("namedStyleType") == "HEADING_1":
            out.append(el)
    return out


def _employee_range(tab: dict, employee: str) -> tuple[int, int] | None:
    """[start, end) of an employee's block (its H1 up to the next H1 or body end)."""
    want = employee.strip().lower()
    heads = _heading1_elements(tab)
    for idx, el in enumerate(heads):
        if _paragraph_text(el).strip().lower() == want:
            start = el["startIndex"]
            if idx + 1 < len(heads):
                end = heads[idx + 1]["startIndex"]
            else:
                end = _body_end_index(tab) - 1  # keep the tab's final newline
            return start, end
    return None


def _manager_section_start(tab: dict, employee: str) -> int | None:
    """Start index of the employee's "Manager Response" H2, if that section exists."""
    rng = _employee_range(tab, employee)
    if not rng:
        return None
    start, end = rng
    body = (tab.get("documentTab", {}) or {}).get("body", {}) or {}
    want = MANAGER_SECTION_TITLE.strip().lower()
    for el in body.get("content", []):
        s = el.get("startIndex")
        if s is None or s < start or s >= end:
            continue
        para = el.get("paragraph")
        if not para:
            continue
        style = (para.get("paragraphStyle", {}) or {}).get("namedStyleType")
        if style == "HEADING_2" and _paragraph_text(el).strip().lower() == want:
            return s
    return None


# --- Building the insert for one connection --------------------------------


HEADER_MANAGER = "Manager-set priorities"
HEADER_ASSOCIATE = "Associate-set priorities"


def _previous_quarter(year: int, quarter: int) -> tuple[int, int]:
    return (year, quarter - 1) if quarter > 1 else (year - 1, 4)


def _previous_title(qc: QuarterlyConnection) -> str:
    if qc.previous is not None:
        y, q = qc.previous.year, qc.previous.quarter
    else:
        y, q = _previous_quarter(qc.year, qc.quarter)
    return f"Previous Priorities ({y}Q{q})"


def _section_paras(qc: QuarterlyConnection) -> list[Para]:
    """The associate's sections/questions/answers (no H1).

    Each answer is translated from its rich-text HTML (bold/italic/links/bullets
    preserved), falling back to plain text.
    """
    paras: list[Para] = []
    for section in qc.sections:
        paras.append(Para(runs=[Run(section.name)], style="HEADING_2"))
        for q in section.questions:
            paras.append(Para(runs=[Run(q.question)], style="HEADING_3"))
            answer = html_to_paras(q.answer_html) or (
                [Para(runs=[Run(q.answer)])] if q.answer.strip() else []
            )
            paras.extend(answer)
    return paras


def _manager_paras(qc: QuarterlyConnection) -> list[Para]:
    """The Manager Response section: an H2 with an empty slot per associate section."""
    paras: list[Para] = [Para(runs=[Run(MANAGER_SECTION_TITLE)], style="HEADING_2")]
    for section in qc.sections:
        paras.append(Para(runs=[Run(section.name)], style="HEADING_3"))
        paras.append(Para(runs=[]))  # empty line for the manager to type into
    return paras


def _range(start: int, end: int, tab_id: str) -> dict:
    return {"startIndex": start, "endIndex": end, "tabId": tab_id}


def _build_requests(
    tab_id: str, start: int, paras: list[Para], trailing_newline: bool = True
) -> list[dict]:
    """batchUpdate requests to insert ``paras`` at ``start`` within ``tab_id``.

    One InsertText with the whole blob, then a paragraph-style request per paragraph,
    a text-style request per emphasized/linked run, and a createParagraphBullets per
    run of consecutive bullet paragraphs. All offsets are deterministic from the known
    insert point and text.

    With ``trailing_newline=False`` (for table cells) the final paragraph gets no
    inserted newline — it borrows the cell's own structural newline as its terminator,
    avoiding a spurious blank line in the cell.
    """
    if trailing_newline:
        text = "".join(p.text + "\n" for p in paras)
    else:
        text = "\n".join(p.text for p in paras)
    requests: list[dict] = [
        {"insertText": {"location": {"index": start, "tabId": tab_id}, "text": text}}
    ]

    # Per-paragraph [start, end) ranges (end always includes a terminating newline —
    # inserted, or, for the last para when trailing_newline is False, the structural one).
    ranges: list[tuple[int, int]] = []
    cursor = start
    last = len(paras) - 1
    for i, p in enumerate(paras):
        ranges.append((cursor, cursor + len(p.text) + 1))
        cursor += len(p.text) + (1 if (trailing_newline or i < last) else 0)

    for p, (s, e) in zip(paras, ranges):
        requests.append(
            {
                "updateParagraphStyle": {
                    "range": _range(s, e, tab_id),
                    "paragraphStyle": {"namedStyleType": p.style},
                    "fields": "namedStyleType",
                }
            }
        )
        run_cursor = s
        for r in p.runs:
            length = len(r.text)
            style: dict = {}
            fields: list[str] = []
            if r.bold:
                style["bold"] = True
                fields.append("bold")
            if r.italic:
                style["italic"] = True
                fields.append("italic")
            if r.link:
                style["link"] = {"url": r.link}
                fields.append("link")
            if length and fields:
                requests.append(
                    {
                        "updateTextStyle": {
                            "range": _range(run_cursor, run_cursor + length, tab_id),
                            "textStyle": style,
                            "fields": ",".join(fields),
                        }
                    }
                )
            run_cursor += length

    # Group consecutive bullet paragraphs into one createParagraphBullets each.
    i = 0
    while i < len(paras):
        if paras[i].bullet:
            j = i
            while j + 1 < len(paras) and paras[j + 1].bullet:
                j += 1
            requests.append(
                {
                    "createParagraphBullets": {
                        "range": _range(ranges[i][0], ranges[j][1], tab_id),
                        "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                    }
                }
            )
            i = j + 1
        else:
            i += 1
    return requests


# --- Table (previous priorities) -------------------------------------------


def _find_table_after(tab: dict, index: int) -> dict | None:
    """The first table structural element at or after ``index`` in the tab body."""
    body = (tab.get("documentTab", {}) or {}).get("body", {}) or {}
    for el in body.get("content", []):
        if "table" in el and el.get("startIndex", -1) >= index:
            return el
    return None


def _cell_content_starts(table_el: dict) -> list[list[int]]:
    """Content start index of each cell, as [[r0c0, r0c1], [r1c0, r1c1]]."""
    out: list[list[int]] = []
    for row in table_el["table"]["tableRows"]:
        out.append([cell["content"][0]["startIndex"] for cell in row["tableCells"]])
    return out


def _write_employee(
    service,
    doc_id: str,
    tab_name: str,
    tab_id: str,
    start: int,
    qc: QuarterlyConnection,
    include_manager: bool,
) -> None:
    """Write one employee's block at ``start``: heading, previous-priorities table, then
    the associate sections and (optionally) the Manager Response section.

    Done in ordered batches with re-fetches, because the table is a structural element
    whose cell indices are only known after it's inserted.
    """

    def b(reqs: list[dict]) -> None:
        if reqs:
            service.documents().batchUpdate(
                documentId=doc_id, body={"requests": reqs}
            ).execute()

    def g() -> dict:
        return service.documents().get(documentId=doc_id, includeTabsContent=True).execute()

    # 1. Heading block before the table (H1 employee, H2 "Previous Priorities (...)").
    pre = [
        Para(runs=[Run(qc.employee)], style="HEADING_1"),
        Para(runs=[Run(_previous_title(qc))], style="HEADING_2"),
    ]
    b(_build_requests(tab_id, start, pre))
    table_index = start + sum(len(p.text) + 1 for p in pre)

    # 2. Insert the empty 2x2 table.
    b([{"insertTable": {"rows": 2, "columns": 2,
                        "location": {"index": table_index, "tabId": tab_id}}}])

    # 3. Fill the cells. Headers always; content cells only when we have previous data.
    tab = find_tab(g(), tab_name)
    table_el = _find_table_after(tab, table_index)
    cells: list[tuple[int, list[Para]]] = []
    if table_el:
        starts = _cell_content_starts(table_el)
        cells.append((starts[0][0], [Para(runs=[Run(HEADER_MANAGER, bold=True)])]))
        cells.append((starts[0][1], [Para(runs=[Run(HEADER_ASSOCIATE, bold=True)])]))
        if qc.previous and qc.previous.manager_html.strip():
            mp = html_to_paras(qc.previous.manager_html)
            if mp:
                cells.append((starts[1][0], mp))
        if qc.previous and qc.previous.associate_html.strip():
            ap = html_to_paras(qc.previous.associate_html)
            if ap:
                cells.append((starts[1][1], ap))
        # Fill highest index first so earlier cells' indices stay valid.
        reqs: list[dict] = []
        for cstart, cparas in sorted(cells, key=lambda c: -c[0]):
            reqs += _build_requests(tab_id, cstart, cparas, trailing_newline=False)
        b(reqs)

    # 4. Associate sections + optional Manager Response section, after the table.
    tab = find_tab(g(), tab_name)
    table_el = _find_table_after(tab, table_index)
    post_index = table_el["endIndex"] if table_el else table_index
    post = _section_paras(qc)
    if include_manager:
        post += _manager_paras(qc)
    b(_build_requests(tab_id, post_index, post))


# --- Sync ------------------------------------------------------------------


class SyncResult:
    def __init__(self) -> None:
        self.created = 0
        self.replaced = 0
        self.skipped = 0

    def __str__(self) -> str:
        return f"created={self.created} replaced={self.replaced} skipped={self.skipped}"


def sync(
    cfg: Config,
    connections: list[QuarterlyConnection],
    dry_run: bool = False,
    replace: bool = False,
) -> SyncResult:
    """Append each connection into its quarter tab.

    An employee already present in the tab is skipped — unless ``replace`` is set, in
    which case their existing block (H1 → next H1) is deleted and rewritten. Replacing
    discards anything previously in that block, including a manager's typed response,
    so it's opt-in.
    """
    tag = "[dry-run] " if dry_run else ""
    service = _service(cfg)
    logger.info("%sWriting %d connection(s) into doc %s%s",
                tag, len(connections), cfg.talent_doc_id,
                " (replace on)" if replace else "")

    def _get_doc() -> dict:
        return (
            service.documents()
            .get(documentId=cfg.talent_doc_id, includeTabsContent=True)
            .execute()
        )

    def _batch(requests: list[dict]) -> None:
        service.documents().batchUpdate(
            documentId=cfg.talent_doc_id, body={"requests": requests}
        ).execute()

    result = SyncResult()
    # Employees we've written this run, so repeated/duplicate connections are
    # deduped even in dry-run, where the live doc doesn't reflect our writes.
    written: set[tuple[str, str]] = set()
    doc = _get_doc()
    for qc in connections:
        # On a real run, re-fetch so a tab that grew/shrank from an earlier employee
        # this run has fresh indices and presence; dry-run reuses the snapshot.
        if not dry_run and (result.created or result.replaced):
            doc = _get_doc()
        tab = find_tab(doc, qc.tab_name)
        if tab is None:
            raise RuntimeError(
                f"No tab named {qc.tab_name!r} in the doc (needed for "
                f"{qc.employee}). Create it by hand first — the Docs API can't "
                f"create tabs. Available tabs: {_available_tab_names(doc) or '(none)'}."
            )
        marker = (qc.tab_name.lower(), qc.employee.strip().lower())
        present = marker in written or _employee_already_present(tab, qc.employee)
        if present and not replace:
            logger.info("%sskip: %s already in tab %s", tag, qc.employee, qc.tab_name)
            result.skipped += 1
            continue

        tab_id = tab["tabProperties"]["tabId"]
        start = _body_end_index(tab) - 1  # default: append before the final newline
        replacing = present and replace
        # When replacing, preserve an existing Manager Response section: rewrite only
        # the associate content (up to that H2). Without one (new employee, or an
        # old-format block), rewrite/insert the full block including that section.
        include_manager = True

        if replacing and not dry_run:
            mgr_start = _manager_section_start(tab, qc.employee)
            rng = _employee_range(tab, qc.employee)
            if rng and mgr_start is not None:
                s = rng[0]
                logger.info("%sreplace: rewriting %s associate content [%d,%d), "
                            "keeping Manager Response", tag, qc.employee, s, mgr_start)
                _batch([{"deleteContentRange": {"range": _range(s, mgr_start, tab_id)}}])
                include_manager = False
            elif rng:
                s, e = rng
                logger.info("%sreplace: rewriting whole %s block [%d,%d)",
                            tag, qc.employee, s, e)
                _batch([{"deleteContentRange": {"range": _range(s, e, tab_id)}}])
            else:
                s = start
            doc = _get_doc()
            tab = find_tab(doc, qc.tab_name)
            tab_id = tab["tabProperties"]["tabId"]
            start = s  # rewrite in place where the old block began

        verb = "replace" if replacing else "create"
        logger.info("%s%s: %s -> tab %s (%d section(s), previous=%s)",
                    tag, verb, qc.employee, qc.tab_name, len(qc.sections),
                    qc.previous.label if qc.previous else "none")
        if not dry_run:
            _write_employee(
                service, cfg.talent_doc_id, qc.tab_name, tab_id, start, qc, include_manager
            )
        written.add(marker)
        if replacing:
            result.replaced += 1
        else:
            result.created += 1

    logger.info("%sDocs write complete: %s", tag, result)
    return result
