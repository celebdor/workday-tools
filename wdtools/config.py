"""Load and validate config.toml, and resolve on-disk paths for secrets/state."""

from __future__ import annotations

import logging
import os
import shutil
import sys
import textwrap
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

APP_DIR_NAME = "workday-tools"

# Written verbatim by `wdtools init`. Kept in sync with config.example.toml.
CONFIG_TEMPLATE = """\
# wdtools configuration. Fill in the CHANGE_ME values.

[workday]
# Your tenant's base URL, e.g. https://wd5.myworkday.com/<tenant>/
home_url = "https://wd5.myworkday.com/CHANGE_ME/d/home.htmld"

# The team absence / time-off calendar view you want to scrape.
# Open it in your browser once and copy the URL from the address bar.
absence_calendar_url = "https://wd5.myworkday.com/CHANGE_ME/d/CHANGE_ME.htmld"

# A substring that only appears in the URL *after* you're fully logged in
# (i.e. not on the SSO/IdP login pages). Used to detect a successful login.
# Often just your tenant path, e.g. "myworkday.com/CHANGE_ME/d/".
login_success_url_substring = "myworkday.com/CHANGE_ME/d/"

[google]
# The calendar ID of the shared team calendar (Google Calendar settings ->
# "Integrate calendar" -> Calendar ID). Looks like an email address.
calendar_id = "CHANGE_ME@group.calendar.google.com"

[optin]
# Calendar sync can be gated on a Google Form. Create it with one single-select
# question (dropdown, multiple choice, or radio) whose options are exactly "No"
# and "Yes", and turn on "Collect email addresses" in the form settings. Paste
# the form ID from its edit URL here:
#   https://docs.google.com/forms/d/<FORM_ID>/edit
# Leave empty to apply `default` to everyone (see below).
form_id = ""

# What to do when someone has no decisive form answer: no submission, an
# unmapped person, or a response without a Yes/No answer. "opt_out" (default)
# keeps them out of the calendar; "opt_in" keeps syncing them until they
# explicitly answer No. An explicit No always opts out.
default = "opt_out"

# Map each respondent's Google email to their exact Workday display name (the
# name shown on absence events), or to their Workday worker id. Unmapped people
# follow `default`.
# [optin.people]
# "person@example.com" = "Person Name"

[talent]
# Optional — only needed for the `qc-*` (Quarterly Connections) commands.
# The Workday "My Tasks" / inbox URL where Quarterly Connection tasks appear.
# Open it in your browser once and copy the URL from the address bar.
my_tasks_url = "https://wd5.myworkday.com/CHANGE_ME/d/inbox"
# The Google Doc that holds the quarterly connection write-ups. Copy its id from
# the URL: https://docs.google.com/document/d/<doc_id>/edit
# NOTE: the per-quarter tabs ("2026Q1 QC", …) must already exist in that doc — the
# Google Docs API cannot create tabs, so the tool fills existing tabs by name.
doc_id = "CHANGE_ME"

[sync]
timezone = "Europe/Madrid"
# Window of days to keep in sync, relative to today.
days_back = 14
days_forward = 120
# If true, prefix every synced event summary with this (helps humans spot
# machine-managed events). Leave empty to disable.
summary_prefix = ""

# Local times (24h "HH:MM") at which the tray app auto-runs a sync.
# A slot that's already passed when the app starts runs once as a catch-up.
sync_times = ["09:30", "15:30"]
"""


def config_dir() -> Path:
    """Directory for config and user-supplied secrets (config.toml, credentials.json).

    Honors ``XDG_CONFIG_HOME`` as a cross-platform override; otherwise follows
    each platform's convention (``~/Library/Application Support`` on macOS,
    ``~/.config`` elsewhere).
    """
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    return Path.home() / ".config" / APP_DIR_NAME


def state_dir() -> Path:
    """Directory for app-managed state (the Workday session, the Google token).

    Per the XDG Base Directory spec these belong in ``XDG_STATE_HOME`` (default
    ``~/.local/state``), not alongside config. macOS has no separate state
    location, so it reuses Application Support.
    """
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    return Path.home() / ".local" / "state" / APP_DIR_NAME


def log_dir() -> Path:
    """Directory for log files.

    macOS has a dedicated convention (``~/Library/Logs``); elsewhere logs are
    state, so they go under ``XDG_STATE_HOME`` (default ``~/.local/state``).
    """
    if sys.platform == "darwin" and not os.environ.get("XDG_STATE_HOME"):
        return Path.home() / "Library" / "Logs" / APP_DIR_NAME
    return state_dir() / "logs"


def default_config_path() -> Path:
    """Where `init` writes, and where commands look for config.toml by default."""
    return config_dir() / "config.toml"


def _migrate(old: Path, new: Path) -> None:
    """Move a state file left behind in the config dir by an earlier version."""
    if old != new and old.exists() and not new.exists():
        new.parent.mkdir(parents=True, exist_ok=True)
        old.replace(new)


_OPTIN_DEFAULT_ALIASES = {
    "opt_out": "opt_out",
    "opt-out": "opt_out",
    "opted_out": "opt_out",
    "opt_in": "opt_in",
    "opt-in": "opt_in",
    "opted_in": "opt_in",
}


def _parse_optin_default(value: object) -> str:
    """Normalize [optin].default, accepting common spelling variants."""
    key = str(value).strip().casefold()
    try:
        return _OPTIN_DEFAULT_ALIASES[key]
    except KeyError:
        raise ValueError(
            '[optin].default must be "opt_out" or "opt_in".'
        ) from None


@dataclass(frozen=True)
class Config:
    # Workday
    workday_home_url: str
    absence_calendar_url: str
    login_success_url_substring: str
    # Google
    calendar_id: str
    # Sync
    timezone: str
    days_back: int
    days_forward: int
    summary_prefix: str
    sync_times: tuple[str, ...]  # local "HH:MM" times the tray app auto-syncs at
    # Paths (persisted between runs; live outside the repo)
    state_path: Path
    google_credentials_path: Path
    google_token_path: Path
    google_docs_token_path: Path
    google_forms_token_path: Path
    qc_cache_path: Path
    # Opt-in gate for calendar sync (optional; empty form_id = use default for everyone)
    optin_form_id: str = ""
    optin_default: str = "opt_out"  # "opt_out" (default) or "opt_in"
    optin_people: dict[str, str] = field(default_factory=dict)
    # Talent / Quarterly Connections (optional; only the `qc-*` commands need these)
    my_tasks_url: str = ""
    talent_doc_id: str = ""

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"{path} not found. Run `wdtools init` to create it, then fill it in."
            )
        with path.open("rb") as fh:
            raw = tomllib.load(fh)

        wd = raw.get("workday", {})
        g = raw.get("google", {})
        s = raw.get("sync", {})
        t = raw.get("talent", {})
        o = raw.get("optin", {})

        people_raw = o.get("people", {}) or {}
        if not isinstance(people_raw, dict):
            raise ValueError(
                "[optin.people] must be a TOML table of email = \"Workday name\" entries."
            )
        optin_people = {
            str(email).strip(): str(person).strip()
            for email, person in people_raw.items()
            if str(email).strip() and str(person).strip()
        }

        cd = config_dir()
        cd.mkdir(parents=True, exist_ok=True)
        sd = state_dir()
        sd.mkdir(parents=True, exist_ok=True)

        # Move state files written to the config dir by earlier versions.
        _migrate(cd / "workday.state.json", sd / "workday.state.json")
        _migrate(cd / "token.json", sd / "token.json")

        cfg = cls(
            workday_home_url=_require(wd, "workday.home_url"),
            absence_calendar_url=_require(wd, "workday.absence_calendar_url"),
            login_success_url_substring=_require(wd, "workday.login_success_url_substring"),
            calendar_id=_require(g, "google.calendar_id"),
            timezone=s.get("timezone", "UTC"),
            days_back=int(s.get("days_back", 14)),
            days_forward=int(s.get("days_forward", 120)),
            summary_prefix=s.get("summary_prefix", ""),
            sync_times=_parse_times(s.get("sync_times", ["09:30", "15:30"])),
            state_path=sd / "workday.state.json",
            google_credentials_path=cd / "credentials.json",
            google_token_path=sd / "token.json",
            google_docs_token_path=sd / "docs_token.json",
            google_forms_token_path=sd / "forms_token.json",
            qc_cache_path=sd / "qc_cache.json",
            optin_form_id=str(o.get("form_id", "") or "").strip(),
            optin_default=_parse_optin_default(o.get("default", "opt_out")),
            optin_people=optin_people,
            my_tasks_url=t.get("my_tasks_url", ""),
            talent_doc_id=t.get("doc_id", ""),
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("Resolved configuration:\n%s", cfg.as_box(path))
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if "CHANGE_ME" in self.workday_home_url + self.absence_calendar_url + self.calendar_id:
            raise ValueError("config.toml still contains CHANGE_ME placeholders — fill them in.")
        self.validate_optin()

    def validate_optin(self) -> None:
        """Check the [optin] values used by calendar sync.

        An empty ``form_id`` is valid and applies ``optin_default`` to everyone.
        A configured form, however, is useless without at least one email ->
        Workday person mapping, since form responses only carry the respondent's
        email address.
        """
        if self.optin_default not in {"opt_out", "opt_in"}:
            raise ValueError('[optin].default must be "opt_out" or "opt_in".')
        if not self.optin_form_id:
            return
        if "CHANGE_ME" in self.optin_form_id:
            raise ValueError("[optin].form_id still contains CHANGE_ME — fill it in.")
        if not self.optin_people:
            raise ValueError(
                "[optin].form_id is set but [optin.people] is empty. Add at least "
                'one mapping like "person@example.com" = "Person Name".'
            )

    def validate_talent(self) -> None:
        """Check the [talent] values the `qc-*` commands need are filled in.

        Kept separate from ``validate`` so calendar-only configs (no [talent]
        section) keep working for `login`/`sync`.
        """
        if not self.my_tasks_url or not self.talent_doc_id:
            raise ValueError(
                "The Quarterly Connections commands need a [talent] section in "
                "config.toml with 'my_tasks_url' and 'doc_id'. Run `wdtools init` "
                "to see the template, or add them to your existing config."
            )
        if "CHANGE_ME" in self.my_tasks_url + self.talent_doc_id:
            raise ValueError("[talent] still contains CHANGE_ME placeholders — fill them in.")

    def as_box(self, source: Path) -> str:
        """Render the resolved config as a bordered, width-wrapped block."""
        optin_rows: list[tuple[str, object]] = [
            ("form_id", self.optin_form_id or "(unset)"),
            ("default", self.optin_default),
        ]
        optin_rows += sorted(self.optin_people.items()) or [
            ("people", "(unset — no responses can be matched)"),
        ]
        sections: list[tuple[str, list[tuple[str, object]]]] = [
            ("source", [("config.toml", source)]),
            (
                "workday",
                [
                    ("home_url", self.workday_home_url),
                    ("absence_calendar_url", self.absence_calendar_url),
                    ("login_success_url_substring", self.login_success_url_substring),
                ],
            ),
            ("google", [("calendar_id", self.calendar_id)]),
            ("optin", optin_rows),
            (
                "talent",
                [
                    ("my_tasks_url", self.my_tasks_url or "(unset)"),
                    ("doc_id", self.talent_doc_id or "(unset)"),
                ],
            ),
            (
                "sync",
                [
                    ("timezone", self.timezone),
                    ("days_back", self.days_back),
                    ("days_forward", self.days_forward),
                    ("summary_prefix", repr(self.summary_prefix)),
                    ("sync_times", ", ".join(self.sync_times)),
                ],
            ),
            (
                "paths",
                [
                    ("state_path", self.state_path),
                    ("google_credentials_path", self.google_credentials_path),
                    ("google_token_path", self.google_token_path),
                    ("google_docs_token_path", self.google_docs_token_path),
                    ("google_forms_token_path", self.google_forms_token_path),
                    ("qc_cache_path", self.qc_cache_path),
                ],
            ),
        ]
        return render_box("Resolved configuration", sections)


def render_box(title: str, sections: list[tuple[str, list[tuple[str, object]]]]) -> str:
    """Draw a bordered box; long values wrap with a hanging indent."""
    width = min(shutil.get_terminal_size((100, 24)).columns, 100)
    inner = max(width - 4, 20)  # room for "│ " … " │"
    labels = [lbl for _, rows in sections for lbl, _ in rows]
    pad = min(max((len(x) for x in labels), default=0), inner - 10)

    body: list[str] = []
    for i, (name, rows) in enumerate(sections):
        if i:
            body.append("")
        body.append(f"[{name}]")
        for label, value in rows:
            line = f"  {label:<{pad}} = {value}"
            wrapped = textwrap.wrap(line, inner, subsequent_indent=" " * (pad + 5))
            body.extend(wrapped or [line])

    top = "┌─ " + title + " " + "─" * max(width - 5 - len(title), 0) + "┐"
    bottom = "└" + "─" * (width - 2) + "┘"
    rows_out = [top]
    for text in body:
        rows_out.append("│ " + text[:inner].ljust(inner) + " │")
    rows_out.append(bottom)
    return "\n".join(rows_out)


def _parse_times(values) -> tuple[str, ...]:
    out: list[str] = []
    for v in values:
        try:
            hh, mm = str(v).split(":")
            h, m = int(hh), int(mm)
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError
        except ValueError:
            raise ValueError(f"Invalid sync time {v!r} — use 24h 'HH:MM', e.g. '09:30'.")
        out.append(f"{h:02d}:{m:02d}")
    return tuple(out)


def _require(section: dict, dotted: str) -> str:
    key = dotted.split(".")[-1]
    val = section.get(key)
    if not val:
        raise ValueError(f"Missing required config value: {dotted}")
    return val
