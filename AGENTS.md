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
- `wdtools/gcal.py` — idempotent reconcile into Google Calendar, gated by the
  Google Form opt-in decisions.
- `wdtools/optin.py` — read Google Form responses (`forms.responses.readonly`),
  match respondent emails to Workday names/worker ids via `[optin.people]`, and
  resolve the latest Yes/No per person. Explicit Yes/No wins; missing, unmapped,
  or no-answer follows `[optin].default` (`opt_out` by default, `opt_in` for
  opt-out behavior).
- `wdtools/qc.py` — scrape Quarterly Connections from My Tasks (navigate task →
  Get Started → evaluation sections), plus a `qc-snapshot` for selector dev.
- `wdtools/gdocs.py` — write Quarterly Connections into a Google Doc's per-quarter
  tabs (idempotent per employee; never clobbers manager responses).
- `wdtools/richtext.py` — translate Workday rich-text HTML (bold/italic/links/bullets)
  into paragraphs/runs for the Docs writer.
- `wdtools/_capture.py` — shared XHR/fetch capture helper used by both snapshots.
- `wdtools/google_setup.py` — `setup-google`: gcloud project + Calendar/Forms/Docs
  APIs + OAuth consent.
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
- [`specs/opt-in.md`](specs/opt-in.md) — the Google Form opt-in gate: form
  requirements, `[optin]` config (including the `opt_out` / `opt_in` default),
  email→Workday mapping, latest-response rules, opt-out deletion reasons, logging,
  and failure safety. **Read before touching `optin.py` or the opt-in
  filtering/deletion path in `gcal.py`.**
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
- **Opt-in gate** (`optin.py` + `gcal.py`): calendar sync only includes absences
  whose effective decision is opted in. An explicit Google Form **Yes** opts in
  and an explicit **No** opts out; missing/unmapped/no-answer follows
  `[optin].default`. The built-in default is `opt_out` (only decisive Yes syncs);
  set `default = "opt_in"` to sync non-responders until they answer No. The form
  has one single-select question (`No`/`Yes`; dropdown or multiple choice) and
  collects respondent emails; `[optin.people]` maps each email to the exact
  Workday display name or worker id. The latest response wins; on a timestamp
  tie, No outranks a no-answer response, which outranks Yes. An empty
  `[optin].form_id` applies `default` to everyone (with the built-in
  `opt_out`, that deletes in-window managed events). A configured form that
  cannot be read aborts the sync before any calendar write. See
  `specs/opt-in.md`.
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
- **Config/state/log dirs** (`config.py`): config + `credentials.json` in the
  config dir (`XDG_CONFIG_HOME`/`~/.config`); session + `token.json` + the Forms
  `forms_token.json` in the state dir (`XDG_STATE_HOME`/`~/.local/state`); macOS
  uses Application Support for both. The tray writes `tray.log` to `log_dir()`
  (`~/.local/state/.../logs`, or `~/Library/Logs/...` on macOS). Opt-in responses
  and the email→name mapping are logged (INFO per response, DEBUG box), so the
  logs contain employee emails/names.

## Running / dev

- Dev checkout uses `uv`: `uv run wdtools <cmd>`. Browser: `uv run playwright
  install chromium` (or `wdtools-playwright-install`).
- Linux tray needs the `tray-linux` extra (PyGObject) + system build/runtime libs
  so pystray uses the AppIndicator backend instead of the menu-less `_xorg`
  fallback; see the README Linux notes. `uv sync --extra tray-linux`.
- `wdtools scrape` prints absences without writing; `wdtools sync --dry-run`
  previews calendar changes, including opt-in decisions and opt-out deletions.
  Add `-v`/`-vv` for progress and debug detail (the `-vv` opt-in box shows the
  form mapping and every response).
- `wdtools setup-google` enables the Forms API (unconditionally) and authorizes
  `forms_token.json` when `[optin].form_id` is set; re-run it after adding a form
  so a scheduled/tray sync doesn't need an interactive consent.
- No test suite yet; validate parser changes against a saved `snapshots/` payload
  (gitignored) and verify end to end with `scrape` / `sync --dry-run`.

## Conventions

- Python 3, standard `logging` (module-level `logger = logging.getLogger(__name__)`);
  user-facing results go to stdout via `print`, progress/diagnostics to logs.
- Secrets and state never live in the repo and are gitignored.
- Commit attribution: follow the attribution lines configured for the session.
