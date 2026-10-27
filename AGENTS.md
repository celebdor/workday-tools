# AGENTS.md

Guidance for AI coding agents working in this repo. Keep it current when behavior
or structure changes.

## What this is

`wdtools` is a pair of one-way Workday → Google utilities (CLI entrypoint
`wdtools` / `python -m wdtools`):

1. scrape the Workday **Team Absence Calendar** → shared **Google Calendar**.
2. scrape **Quarterly Connection** reviews from Workday **My Tasks** → a shared
   **Google Doc** (the `qc-*` commands).

## Layout

- `wdtools/__main__.py` — CLI: `init`, `login`, `setup-google`, `snapshot`,
  `scrape`, `sync`, `qc-snapshot`, `qc-scrape`, `qc-sync`, `tray`. Sets up logging
  (`-v`/`-vv`).
- `wdtools/config.py` — `config.toml` loading; XDG dirs; `init` template; a
  `-vv` config dump box.
- `wdtools/auth.py` — Workday session via Playwright (interactive login once,
  reused headlessly).
- `wdtools/workday.py` — open the prompted calendar, switch to Month view, page
  month-by-month, parse the `flowController` JSON into `AbsenceEvent`s.
- `wdtools/gcal.py` — idempotent reconcile into Google Calendar.
- `wdtools/qc.py` — scrape Quarterly Connections from My Tasks (navigate task →
  Get Started → evaluation sections), plus a `qc-snapshot` for selector dev.
- `wdtools/gdocs.py` — write Quarterly Connections into a Google Doc's per-quarter
  tabs (idempotent per employee; never clobbers manager responses).
- `wdtools/richtext.py` — translate Workday rich-text HTML (bold/italic/links/bullets)
  into paragraphs/runs for the Docs writer.
- `wdtools/_capture.py` — shared XHR/fetch capture helper used by both snapshots.
- `wdtools/google_setup.py` — `setup-google`: gcloud project + Calendar/Docs APIs +
  OAuth consent.
- `wdtools/models.py` — `AbsenceEvent` (+ stable `wdKey`) and the
  `QuarterlyConnection`/`QCSection`/`QCQuestion` models.
- `wdtools/trayapp.py` — menu-bar/tray scheduler.
- `specs/` — design docs (see below).

## Specs

Design intent that isn't obvious from code lives in `specs/`. Read the relevant
spec before changing that area, and update it when you change the behavior.

- [`specs/reconciliation.md`](specs/reconciliation.md) — how calendar sync tags,
  identifies, and reconciles events (ownership tag, `wdKey`, three-way diff,
  windowing, safety properties). **Read before touching `gcal.py` or
  `AbsenceEvent.key`.**
- [`specs/quarterly-connections.md`](specs/quarterly-connections.md) — the My Tasks
  → task → sections scrape, the `YEARQX QC` tab mapping, the doc layout, and the
  "tabs can't be created via the API / never overwrite manager responses"
  constraints. **Read before touching `qc.py` or `gdocs.py`.**

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
- **Quarterly Connections** (`qc.py` / `gdocs.py`): see the spec. Discovery parses
  the inbox `queryTasks` JSON (employee/quarter/task-URI); the editor is a task
  wizard read via `verticalNavigationItem` (sections) + `richTextContent` (question,
  associate answer). Each employee's block also starts with a **Previous Priorities**
  table carried from their last completed QC (instance moniker → event view → associate
  related-actions menu JSON → View Associate Reviews grid → previous summary; rendered
  with structural `insertTable` in `_write_employee`). Selectors are tenant-specific —
  re-derive from `qc-snapshot` output if they differ. The Google Docs API **cannot
  create tabs**, so the
  per-quarter `YEARQX QC` tabs are created by hand; the tool fills existing tabs and
  skips any employee already present (never overwriting manager responses). Scraped
  connections are cached in the state dir (`qc_cache.json`); `--refresh` re-scrapes,
  `--offline` works purely from the cache.
- **Config/state/log dirs** (`config.py`): config + `credentials.json` live in
  `XDG_CONFIG_HOME`/`~/.config` on every platform; session + `token.json` live in
  `XDG_STATE_HOME`/`~/.local/state` (or Application Support on macOS). The tray
  writes `tray.log` to `log_dir()` (`~/.local/state/.../logs`, or
  `~/Library/Logs/...` on macOS).

## Running / dev

- Dev checkout uses `uv`: `uv run wdtools <cmd>`. Browser: `uv run playwright
  install chromium` (or `wdtools-playwright-install`).
- Linux tray needs the `tray-linux` extra (PyGObject) + system build/runtime libs
  so pystray uses the AppIndicator backend instead of the menu-less `_xorg`
  fallback; see the README Linux notes. `uv sync --extra tray-linux`.
- `wdtools scrape` prints absences without writing; `wdtools sync --dry-run`
  previews calendar changes. Add `-v`/`-vv` for progress and debug detail.
- No test suite yet; validate parser changes against a saved `snapshots/` payload
  (gitignored) and verify end to end with `scrape` / `sync --dry-run`.

## Conventions

- Python 3, standard `logging` (module-level `logger = logging.getLogger(__name__)`);
  user-facing results go to stdout via `print`, progress/diagnostics to logs.
- Secrets and state never live in the repo and are gitignored.
- Commit attribution: follow the attribution lines configured for the session.
