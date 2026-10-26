"""Translate Workday's rich-text HTML into a flat paragraph model for Google Docs.

Workday renders a rich-text answer as ProseMirror HTML — a small, predictable
vocabulary: ``<p>`` / ``<br>`` for paragraphs and soft breaks, ``<b>``/``<strong>``
and ``<i>``/``<em>`` for inline emphasis, ``<h2>``/``<h3>`` for (bold) sub-headings,
and ``<ul>``/``<li>`` for bullet lists (no ordered lists or nesting observed).

The Docs API can't accept HTML, so we parse it into :class:`Para` / :class:`Run`
objects that :mod:`wdtools.gdocs` turns into ``batchUpdate`` requests. Sub-headings
inside an answer map to *bold paragraphs* (not Docs heading styles) so they don't
pollute the document's employee/section/question outline. Empty spacer paragraphs
(ProseMirror emits ``<p><br></p>`` between blocks) are dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser


@dataclass
class Run:
    text: str
    bold: bool = False
    italic: bool = False
    link: str | None = None


@dataclass
class Para:
    runs: list[Run] = field(default_factory=list)
    style: str = "NORMAL_TEXT"  # or HEADING_1/HEADING_2/HEADING_3
    bullet: bool = False

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)


_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paras: list[Para] = []
        self._runs: list[Run] = []
        self._open = False
        self._bold = 0
        self._ital = 0
        self._in_li = False
        self._bullet = False
        self._force_bold = False
        self._hrefs: list[str] = []

    def _flush(self) -> None:
        if self._open:
            if any(r.text.strip() for r in self._runs):
                self.paras.append(_normalize(Para(runs=self._runs, bullet=self._bullet)))
            self._runs = []
            self._open = False

    def _start(self, bullet: bool, force_bold: bool) -> None:
        self._flush()
        self._open = True
        self._runs = []
        self._bullet = bullet
        self._force_bold = force_bold

    def handle_starttag(self, tag, attrs):
        if tag in ("b", "strong"):
            self._bold += 1
        elif tag in ("i", "em"):
            self._ital += 1
        elif tag == "a":
            self._hrefs.append(dict(attrs).get("href") or "")
        elif tag == "li":
            self._in_li = True
        elif tag == "p":
            self._start(self._in_li, False)
        elif tag in _HEADINGS:
            self._start(False, True)
        elif tag == "br":
            # Soft break: end the line, continue in the same context.
            bullet, force_bold = self._bullet, self._force_bold
            self._flush()
            self._open = True
            self._runs = []
            self._bullet = bullet
            self._force_bold = force_bold

    def handle_endtag(self, tag):
        if tag in ("b", "strong"):
            self._bold = max(0, self._bold - 1)
        elif tag in ("i", "em"):
            self._ital = max(0, self._ital - 1)
        elif tag == "a":
            if self._hrefs:
                self._hrefs.pop()
        elif tag == "li":
            self._in_li = False
            self._flush()
        elif tag == "p" or tag in _HEADINGS or tag in ("ul", "ol"):
            self._flush()

    def handle_data(self, data):
        if not data:
            return
        if not self._open:
            self._start(self._in_li, False)
        self._runs.append(
            Run(
                text=data,
                bold=self._bold > 0 or self._force_bold,
                italic=self._ital > 0,
                link=self._hrefs[-1] if self._hrefs else None,
            )
        )


def _normalize(para: Para) -> Para:
    """Collapse whitespace, merge adjacent same-style runs, strip the paragraph."""
    merged: list[Run] = []
    for r in para.runs:
        text = re.sub(r"\s+", " ", r.text)
        if not text:
            continue
        if (
            merged
            and merged[-1].bold == r.bold
            and merged[-1].italic == r.italic
            and merged[-1].link == r.link
        ):
            merged[-1].text += text
        else:
            merged.append(Run(text=text, bold=r.bold, italic=r.italic, link=r.link))
    if merged:
        merged[0].text = merged[0].text.lstrip()
        merged[-1].text = merged[-1].text.rstrip()
    para.runs = [r for r in merged if r.text]
    return para


def html_to_paras(html: str) -> list[Para]:
    """Parse rich-text HTML into non-empty paragraphs. Empty/plain input -> []."""
    if not html or not html.strip():
        return []
    if "<" not in html:  # plain text
        text = html.strip()
        return [Para(runs=[Run(text=text)])] if text else []
    p = _Parser()
    p.feed(html)
    p._flush()
    return [para for para in p.paras if para.runs]
