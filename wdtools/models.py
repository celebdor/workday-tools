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
