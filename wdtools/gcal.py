"""Idempotent sync of AbsenceEvents into a Google Calendar via the official API.

Every event this tool creates is stamped with an extended property so we can
find exactly the events we manage and converge the calendar to match Workday —
creating, updating, and deleting as needed — without ever touching events people
added by hand, and without creating duplicates on re-runs.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .config import Config
from .models import AbsenceEvent

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
SOURCE_TAG = "workday-sync"  # marks events we own, via extendedProperties.private


def _credentials(cfg: Config) -> Credentials:
    creds: Credentials | None = None
    if Path(cfg.google_token_path).exists():
        creds = Credentials.from_authorized_user_file(str(cfg.google_token_path), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not Path(cfg.google_credentials_path).exists():
            raise FileNotFoundError(
                f"Missing Google OAuth client at {cfg.google_credentials_path}. "
                "Create an OAuth client (Desktop app) in Google Cloud Console and "
                "save the downloaded JSON there. See README."
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(cfg.google_credentials_path), SCOPES
        )
        creds = flow.run_local_server(port=0)
    Path(cfg.google_token_path).write_text(creds.to_json(), encoding="utf-8")
    return creds


def _service(cfg: Config):
    return build("calendar", "v3", credentials=_credentials(cfg), cache_discovery=False)


def _to_gcal_body(ev: AbsenceEvent, cfg: Config) -> dict:
    summary = ev.summary
    if cfg.summary_prefix:
        summary = f"{cfg.summary_prefix}{summary}"
    return {
        "summary": summary,
        "start": {"date": ev.start.isoformat()},
        "end": {"date": ev.gcal_end_exclusive.isoformat()},
        "transparency": "transparent",  # don't block people's free/busy
        "extendedProperties": {
            "private": {"source": SOURCE_TAG, "wdKey": ev.key},
        },
    }


def _fetch_managed(service, cfg: Config, window_start: date, window_end: date) -> dict[str, dict]:
    """Return {wdKey: event} for events we manage within the window."""
    managed: dict[str, dict] = {}
    page_token = None
    while True:
        resp = (
            service.events()
            .list(
                calendarId=cfg.calendar_id,
                privateExtendedProperty=f"source={SOURCE_TAG}",
                timeMin=f"{window_start.isoformat()}T00:00:00Z",
                timeMax=f"{window_end.isoformat()}T00:00:00Z",
                singleEvents=True,
                showDeleted=False,
                maxResults=2500,
                pageToken=page_token,
            )
            .execute()
        )
        for item in resp.get("items", []):
            key = item.get("extendedProperties", {}).get("private", {}).get("wdKey")
            if key:
                managed[key] = item
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return managed


def _needs_update(existing: dict, desired: dict) -> bool:
    return (
        existing.get("summary") != desired["summary"]
        or existing.get("start", {}).get("date") != desired["start"]["date"]
        or existing.get("end", {}).get("date") != desired["end"]["date"]
    )


class SyncResult:
    def __init__(self) -> None:
        self.created = 0
        self.updated = 0
        self.deleted = 0
        self.unchanged = 0

    def __str__(self) -> str:
        return (
            f"created={self.created} updated={self.updated} "
            f"deleted={self.deleted} unchanged={self.unchanged}"
        )


def sync(cfg: Config, absences: list[AbsenceEvent], dry_run: bool = False) -> SyncResult:
    service = _service(cfg)
    window_start = date.today() - timedelta(days=cfg.days_back)
    window_end = date.today() + timedelta(days=cfg.days_forward)

    # Only sync absences that fall inside the window (end >= window_start,
    # start < window_end), so we don't delete out-of-window managed events.
    desired = {
        ev.key: ev
        for ev in absences
        if ev.end >= window_start and ev.start < window_end
    }
    existing = _fetch_managed(service, cfg, window_start, window_end)

    result = SyncResult()

    for key, ev in desired.items():
        body = _to_gcal_body(ev, cfg)
        if key not in existing:
            if not dry_run:
                service.events().insert(calendarId=cfg.calendar_id, body=body).execute()
            result.created += 1
        elif _needs_update(existing[key], body):
            if not dry_run:
                service.events().update(
                    calendarId=cfg.calendar_id,
                    eventId=existing[key]["id"],
                    body=body,
                ).execute()
            result.updated += 1
        else:
            result.unchanged += 1

    for key, item in existing.items():
        if key not in desired:
            if not dry_run:
                service.events().delete(
                    calendarId=cfg.calendar_id, eventId=item["id"]
                ).execute()
            result.deleted += 1

    return result
