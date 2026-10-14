# workday-tools

Browser-driven tooling for Workday (no API keys required) plus clean,
API-based writes to Google Calendar.

First utility: **sync the team absence calendar from Workday into a shared Google
Calendar.** Idempotent — safe to run on a schedule; it converges the calendar to
match Workday instead of piling up duplicates, and never touches events people
added by hand.

## How it works

- **Read side — Workday:** [Playwright](https://playwright.dev/python/) drives a
  real Chromium. You log in by hand *once* (SSO + MFA) in a visible window; the
  authenticated session is saved to disk and reused headlessly after that, until
  it expires.
- **Write side — Google Calendar:** the official Google Calendar API over OAuth.
  Every event the tool creates is stamped with a private extended property
  (`source=workday-sync`) so sync can safely create / update / delete only its
  own events.

```
Workday (Playwright, headless)  ->  AbsenceEvent[]  ->  Google Calendar API
```

## Install

The project is managed with [uv](https://docs.astral.sh/uv/). uv provisions the
right Python (3.11+) and all dependencies for you. Install uv first:

```bash
# macOS (Homebrew — the native way)
brew install uv

# Linux (or macOS without Homebrew)
curl -LsSf https://astral.sh/uv/install.sh | sh   # Fedora: sudo dnf install uv
```

Everything after this is the same on macOS and Linux; the only platform-specific
step is the Linux GTK libraries noted under "Linux only".

### As a user (recommended)

Install the CLI/tray app globally, then fetch the browser Playwright drives:

```bash
uv tool install git+https://github.com/celebdor/workday-tools
wdtools-playwright-install    # one-time Chromium download (~150 MB)
```

This puts `wdtools` (and `wdtools-playwright-install`) on your PATH. To upgrade
later: `uv tool upgrade workday-tools`. To try it without installing:
`uvx --from git+https://github.com/celebdor/workday-tools wdtools --help`.

**macOS** — nothing more to install; the tray app runs in the menu bar out of
the box.

**Linux only** — the tray app needs system GTK/AppIndicator libraries (uv can't
install these; they're not Python packages):
- Fedora: `sudo dnf install libappindicator-gtk3 python3-gobject`
- Debian/Ubuntu: `sudo apt install gir1.2-appindicator3-0.1 python3-gi`
- GNOME also needs the *AppIndicator and KStatusNotifierItem* shell extension.

### From a checkout (development)

```bash
git clone https://github.com/celebdor/workday-tools && cd workday-tools
uv sync                       # creates .venv with all deps
uv run playwright install chromium
```

Then run any command with `uv run wdtools …` (e.g. `uv run wdtools sync`).

## Configure

```bash
uv run wdtools init
# writes a config.toml template to the platform config dir
# (~/.config/workday-tools/ on Linux, ~/Library/Application Support/workday-tools/
# on macOS; override the location with XDG_CONFIG_HOME or `--config`), then edit
# it — tenant URLs, the absence calendar URL, and the Google calendar ID of the
# shared team calendar
```

All commands read this default location, so no `--config` flag is needed for
day-to-day use.

## Google OAuth client

```bash
uv run wdtools setup-google
```

This drives the whole Google side with `gcloud`: it selects (or creates) a
project, enables the Calendar API, walks you through the one manual step Google
has no API for — creating a *Desktop app* OAuth client and downloading its JSON —
saves it to `~/.config/workday-tools/credentials.json`, then runs the consent
flow (caching the token at `~/.local/state/workday-tools/token.json`) and
verifies access to your `calendar_id`.

Prerequisites: the Google Cloud SDK (`gcloud`) installed and `gcloud auth login`
done. Set `calendar_id` in your `config.toml` first, so the final verification
can run.

Prefer to do it by hand? Create an OAuth client of type *Desktop app* in the
Cloud Console, download the JSON to `~/.config/workday-tools/credentials.json`;
the first `sync` will open a browser to authorize.

## Menu bar / tray app (recommended daily driver)

Instead of cron, run the tray app — it stays in the menu bar (macOS) or system
tray (Linux), auto-syncs at the configured `sync_times`, and encodes its state in
the icon color:

| Color | Meaning |
|-------|---------|
| 🟢 green | logged in, idle — session is good |
| 🔵 blue | syncing now |
| 🟠 amber | needs login (no/expired session) — use **Log in to Workday…** |
| 🔴 red | last run errored (details in the menu) |
| ⚪ grey | starting up |

```bash
wdtools tray          # or: uv run wdtools tray  (from a checkout)
```

Menu: live status + last-sync result, **Sync now**, **Log in to Workday…**, Quit.
It re-checks the session every 30 min so an expiry turns the icon amber on its own.

Because the tray runs unattended, it also writes a rotating log file (handy when
an overnight sync fails) to the platform log location — `~/.local/state/workday-tools/logs/tray.log`
on Linux, `~/Library/Logs/workday-tools/tray.log` on macOS. It logs at INFO by
default; add `-v`/`-vv` (`wdtools tray -vv`) to also raise the console level and
capture debug detail in the file.

**Platform notes**
- **macOS:** works out of the box in the menu bar.
- **Linux:** needs a StatusNotifier/AppIndicator host. GNOME requires the
  *AppIndicator and KStatusNotifierItem* extension; KDE and most others work
  natively. `pystray` also needs `gobject-introspection`/`gir1.2-appindicator3`
  (Debian/Ubuntu) or `libappindicator-gtk3` + `python3-gobject` (Fedora).
- **Autostart:** add it to your desktop's startup apps, or a systemd
  `--user` service / macOS LaunchAgent, so it's always present.

## CLI usage

From a checkout, prefix each command with `uv run` (e.g. `uv run wdtools login`).

```bash
# Log in once (opens a visible browser; session is saved afterwards)
wdtools login

# Capture the rendered absence page so we can wire up selectors (see below)
wdtools snapshot

# Scrape and print absences without writing anything
wdtools scrape

# Full sync into Google Calendar
wdtools sync            # add --dry-run to preview changes
```

Any command auto-triggers the login flow if there's no valid saved session, so
in practice you can just run `sync`.

Add `-v` to any command to see timestamped progress (opening the calendar, paging,
entries parsed, each create/update/delete), or `-vv` for debug-level detail
including third-party libraries — handy for watching a long sync or diagnosing one:

```bash
wdtools sync --dry-run -v
```

## Adapting to another Workday tenant

The scraper (`wdtools/workday.py`) is wired against one specific Workday tenant: it dismisses
the calendar's parameter prompt, switches to Month view, pages month-by-month, and
parses the `flowController` JSON into absences. Workday markup is tenant-specific
and obfuscated, so another tenant may need the selectors or parsing adjusted.

1. `wdtools snapshot` — dumps the rendered HTML, a screenshot, and every XHR/JSON
   response (with a `*-xhr-index.json` manifest) to `./snapshots`. Add
   `--open-view-menu` to also capture the interval dropdown's markup.
2. Find the response/elements carrying each absence (**person**, **type**,
   **start/end dates**) and adjust `SELECTORS` / the parser in
   `wdtools/workday.py`.

**Shortcut worth checking first:** some Workday tenants expose an iCal /
"Export to Calendar" / "Subscribe" link on time-off views. If yours does, that
feed is far more stable than scraping — parsing it instead is worth it.

## Scheduling

For a desktop, prefer the **tray app** above — it has its own scheduler and shows
status. For a headless server, run `sync` on a timer, e.g. cron:

```cron
# every weekday at 07:30 (wdtools on PATH via `uv tool install`)
30 7 * * 1-5  wdtools sync --config ~/.config/workday-tools/config.toml >> ~/sync.log 2>&1
```

If the saved Workday session has expired, a scheduled run will fail (it can't
pop a login window unattended) — just run `wdtools login` again.

## Security notes

- Nothing lives in the repo. Config and the user-supplied Google client secret
  go in the config dir (`~/.config/workday-tools/`); the saved Workday session
  and the Google token — app-managed state — go in the state dir
  (`~/.local/state/workday-tools/`, per the XDG spec). macOS keeps both under
  `~/Library/Application Support/workday-tools/`. Nothing stores your Workday
  password.
- The saved session and tokens are live credentials — treat those directories
  like an SSH key.
```
