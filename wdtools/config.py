"""Load and validate config.toml, and resolve on-disk paths for secrets/state."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


def _state_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "workday-tools"


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

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"{path} not found. Copy config.example.toml to config.toml and fill it in."
            )
        with path.open("rb") as fh:
            raw = tomllib.load(fh)

        wd = raw.get("workday", {})
        g = raw.get("google", {})
        s = raw.get("sync", {})

        sd = _state_dir()
        sd.mkdir(parents=True, exist_ok=True)

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
            google_credentials_path=sd / "credentials.json",
            google_token_path=sd / "token.json",
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if "CHANGE_ME" in self.workday_home_url + self.absence_calendar_url + self.calendar_id:
            raise ValueError("config.toml still contains CHANGE_ME placeholders — fill them in.")


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
