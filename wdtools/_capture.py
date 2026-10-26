"""Shared helpers for capturing a Workday page's XHR/fetch traffic for selector dev.

Workday renders its grids and task pages from JSON served over XHR/fetch rather
than in the initial HTML, so both the absence-calendar snapshot (``workday.py``)
and the Quarterly Connection snapshot (``qc.py``) dump those responses to disk to
re-derive tenant-specific selectors and payload shapes. The capture logic is the
same for both, so it lives here.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Iterator

from playwright.sync_api import Page, Response

logger = logging.getLogger(__name__)


class NetworkCapture:
    """Collect XHR/fetch responses seen while a page loads.

    We only hold lightweight request metadata plus the live ``Response`` object
    during load; bodies are read later (``dump``), while we're still parked on the
    page so Playwright can still serve them.
    """

    def __init__(self, resource_types: tuple[str, ...] = ("xhr", "fetch")) -> None:
        self._entries: list[dict] = []
        # Workday serves most data as xhr/fetch, but some embedded views (e.g. the
        # task-wizard's embeddedEdit) load as a document navigation — pass a wider
        # set (e.g. +"document") to capture those too.
        self._resource_types = resource_types

    def attach(self, page: Page) -> None:
        page.on("response", self._on_response)

    def _on_response(self, response: Response) -> None:
        # This runs inside Playwright's event loop; an exception here poisons the
        # connection and breaks later page calls, so never let one escape.
        try:
            req = response.request
            if req.resource_type not in self._resource_types:
                return
            self._entries.append(
                {
                    "url": response.url,
                    "status": response.status,
                    "method": req.method,
                    "resource_type": req.resource_type,
                    "content_type": (response.headers or {}).get("content-type", ""),
                    "post_data": safe_post_data(req),
                    "_response": response,
                }
            )
        except Exception:
            pass

    def json_payloads(self) -> Iterator[dict]:
        """Parsed JSON bodies of every captured response (unparseable ones skipped)."""
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
        return f"{len(self._entries)} XHR/fetch response(s) captured"

    def dump(self, out_dir: Path, stamp: str, prefix: str = "capture") -> Path:
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
            fname = f"{prefix}-{stamp}-xhr-{i:03d}-{slug(entry['url'])}.json"
            (out_dir / fname).write_text(body, encoding="utf-8")
            index.append({**entry, "bytes": len(body), "file": fname})

        index_path = out_dir / f"{prefix}-{stamp}-xhr-index.json"
        index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
        return index_path


def safe_post_data(request) -> str | None:
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


def slug(url: str) -> str:
    """A short, filesystem-safe hint derived from a URL's path tail."""
    path = re.sub(r"https?://[^/]+", "", url).split("?", 1)[0]
    tail = "-".join(p for p in path.split("/") if p)[-60:]
    return re.sub(r"[^A-Za-z0-9._-]", "_", tail) or "root"
