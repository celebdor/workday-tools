# AGENTS.md

Guidance for AI coding agents working in this repo. Keep it current when behavior
or structure changes.

## What this is

`wdtools` scrapes the Workday **Team Absence Calendar** and syncs it into a shared
**Google Calendar** (one-way: Workday → Google). CLI entrypoint `wdtools`
(`python -m wdtools`).

## Layout

- `wdtools/__main__.py` — CLI: `init`, `login`, `setup-google`, `snapshot`,
  `scrape`, `sync`, `tray`. Sets up logging (`-v`/`-vv`).
- `wdtools/config.py` — `config.toml` loading; XDG dirs; `init` template; a
  `-vv` config dump box.
- `wdtools/auth.py` — Workday session via Playwright (interactive login once,
  reused headlessly).
- `wdtools/workday.py` — open the prompted calendar, switch to Month view, page
  month-by-month, parse the `flowController` JSON into `AbsenceEvent`s.
- `wdtools/gcal.py` — idempotent reconcile into Google Calendar.
- `wdtools/google_setup.py` — `setup-google`: gcloud project/API + OAuth consent.
- `wdtools/models.py` — `AbsenceEvent` + its stable `wdKey` identity.
- `wdtools/trayapp.py` — menu-bar/tray scheduler.
- `specs/` — design docs (see below).

## Specs

Design intent that isn't obvious from code lives in `specs/`. Read the relevant
spec before changing that area, and update it when you change the behavior.

- [`specs/reconciliation.md`](specs/reconciliation.md) — how calendar sync tags,
  identifies, and reconciles events (ownership tag, `wdKey`, three-way diff,
  windowing, safety properties). **Read before touching `gcal.py` or
  `AbsenceEvent.key`.**

## Key design points

- **Scraping** (`workday.py`): the calendar is a *prompted report* — dismiss the
  parameter dialog (`wd-CommandButton_uic_okButton`), then switch the interval to
  **Month** so each paging click is one month. Paging advances are detected from
  the on-screen period heading (`dateRangeTitle`) changing, not from a named
  network response. Entries are parsed from captured `flowController` JSON
  (`calendarEntry` widgets); only approved time off + holidays are kept, grouped
  into contiguous-day events. Selectors are tenant-specific; re-derive via
  `wdtools snapshot` (add `--open-view-menu` to capture the interval dropdown).
- **Reconciliation** (`gcal.py`): see the spec. Never touch untagged events.
- **Config/state/log dirs** (`config.py`): config + `credentials.json` in the
  config dir (`XDG_CONFIG_HOME`/`~/.config`); session + `token.json` in the state
  dir (`XDG_STATE_HOME`/`~/.local/state`); macOS uses Application Support for both.
  The tray writes `tray.log` to `log_dir()` (`~/.local/state/.../logs`, or
  `~/Library/Logs/...` on macOS).

## Running / dev

- Dev checkout uses `uv`: `uv run wdtools <cmd>`. Browser: `uv run playwright
  install chromium` (or `wdtools-playwright-install`).
- `wdtools scrape` prints absences without writing; `wdtools sync --dry-run`
  previews calendar changes. Add `-v`/`-vv` for progress and debug detail.
- No test suite yet; validate parser changes against a saved `snapshots/` payload
  (gitignored) and verify end to end with `scrape` / `sync --dry-run`.

## Conventions

- Python 3, standard `logging` (module-level `logger = logging.getLogger(__name__)`);
  user-facing results go to stdout via `print`, progress/diagnostics to logs.
- Secrets and state never live in the repo and are gitignored.
- Commit attribution: follow the attribution lines configured for the session.
