"""Normalized domain model shared between the Workday and Google Calendar sides."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class AbsenceEvent:
    """A single person's absence, normalized away from Workday's representation.

    ``start`` and ``end`` are both *inclusive* calendar dates (the first and last
    day the person is away). All-day semantics only — Workday absences are
    day-granular, not timed.
    """

    person: str
    absence_type: str
    start: date
    end: date
    person_id: str | None = None

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"end {self.end} precedes start {self.start} for {self.person}")

    @property
    def key(self) -> str:
        """Stable identity used to match a Workday absence to a calendar event.

        Deliberately excludes mutable display text so re-runs converge instead of
        creating duplicates. Changing any of these fields is treated as a
        delete + create (a genuinely different absence).
        """
        basis = "|".join(
            [
                (self.person_id or self.person).strip().lower(),
                self.absence_type.strip().lower(),
                self.start.isoformat(),
                self.end.isoformat(),
            ]
        )
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:20]

    @property
    def summary(self) -> str:
        return f"{self.person} — {self.absence_type}"

    @property
    def gcal_end_exclusive(self) -> date:
        """Google all-day events use an *exclusive* end date."""
        return self.end + timedelta(days=1)


# --- Quarterly Connections -------------------------------------------------
#
# A separate utility scrapes Workday "My Tasks" for Quarterly Connection reviews
# and writes the associate's answers into a Google Doc. These models normalize one
# connection away from Workday's page structure.


def quarter_for(month: int) -> int:
    """Calendar quarter (1-4) for a 1-12 month. Q1 Jan-Mar … Q4 Oct-Dec."""
    if not 1 <= month <= 12:
        raise ValueError(f"month out of range: {month}")
    return (month - 1) // 3 + 1


@dataclass(frozen=True)
class QCQuestion:
    """A single prompt in a Quarterly Connection section and the associate's answer.

    ``answer`` is the plain-text rendition (for console output); ``answer_html`` is
    the original rich-text HTML, which :mod:`wdtools.gdocs` translates to preserve
    bold/italic/links/lists when writing to the Google Doc.
    """

    question: str
    answer: str
    answer_html: str = ""


@dataclass(frozen=True)
class QCSection:
    """One left-nav section of the manager evaluation (e.g. "Accomplishments")."""

    name: str
    questions: tuple[QCQuestion, ...]


@dataclass(frozen=True)
class QuarterlyConnection:
    """One employee's Quarterly Connection, normalized from the Workday task.

    ``year``/``quarter`` come from the review period and name the target Google Doc
    tab (``tab_name``); ``period_label`` keeps Workday's raw text for diagnostics.
    """

    employee: str
    year: int
    quarter: int
    sections: tuple[QCSection, ...]
    period_label: str = ""

    @property
    def tab_name(self) -> str:
        """The Google Doc tab this connection belongs in, e.g. '2026Q1 QC'."""
        return f"{self.year}Q{self.quarter} QC"
