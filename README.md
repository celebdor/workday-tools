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

## Setup

1. **Install:**
   ```bash
   python -m venv .venv && . .venv/bin/activate
   pip install -r requirements.txt
   playwright install chromium
   ```

2. **Configure:**
   ```bash
   cp config.example.toml config.toml
   # edit config.toml — tenant URLs, the absence calendar URL, and the Google
   # calendar ID of the shared team calendar
   ```

3. **Google OAuth client:** in Google Cloud Console create an OAuth client of
   type *Desktop app*, download the JSON, and save it as
   `~/.config/workday-tools/credentials.json`. First `sync` opens a browser to
   authorize; the resulting token is cached at
   `~/.config/workday-tools/token.json`.

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
python -m wdtools tray
```

Menu: live status + last-sync result, **Sync now**, **Log in to Workday…**, Quit.
It re-checks the session every 30 min so an expiry turns the icon amber on its own.

**Platform notes**
- **macOS:** works out of the box in the menu bar.
- **Linux:** needs a StatusNotifier/AppIndicator host. GNOME requires the
  *AppIndicator and KStatusNotifierItem* extension; KDE and most others work
  natively. `pystray` also needs `gobject-introspection`/`gir1.2-appindicator3`
  (Debian/Ubuntu) or `libappindicator-gtk3` + `python3-gobject` (Fedora).
- **Autostart:** add it to your desktop's startup apps, or a systemd
  `--user` service / macOS LaunchAgent, so it's always present.

## CLI usage

```bash
# Log in once (opens a visible browser; session is saved afterwards)
python -m wdtools login

# Capture the rendered absence page so we can wire up selectors (see below)
python -m wdtools snapshot

# Scrape and print absences without writing anything
python -m wdtools scrape

# Full sync into Google Calendar
python -m wdtools sync            # add --dry-run to preview changes
```

Any command auto-triggers the login flow if there's no valid saved session, so
in practice you can just run `sync`.

## One manual step remaining: Workday selectors

Workday's absence calendar markup is tenant-specific and obfuscated, so the
scraper's selectors (`wdtools/workday.py`) start as a documented placeholder and
need wiring to your actual view:

1. `python -m wdtools snapshot` — dumps rendered HTML + a screenshot to
   `./snapshots`.
2. Inspect the HTML to find how each absence encodes **person**, **type**, and
   **start/end dates**.
3. Update `SELECTORS` and `_parse_events` in `wdtools/workday.py`.

**Shortcut worth checking first:** some Workday tenants expose an iCal /
"Export to Calendar" / "Subscribe" link on time-off views. If yours does, that
feed is far more stable than scraping — tell me and we'll parse the feed instead.

## Scheduling

For a desktop, prefer the **tray app** above — it has its own scheduler and shows
status. For a headless server, run `sync` on a timer, e.g. cron:

```cron
# every weekday at 07:30
30 7 * * 1-5  cd /path/to/workday-tools && .venv/bin/python -m wdtools sync >> sync.log 2>&1
```

If the saved Workday session has expired, a scheduled run will fail (it can't
pop a login window unattended) — just run `python -m wdtools login` again.

## Security notes

- `config.toml`, the saved session, and all Google secrets live outside the repo
  (in `~/.config/workday-tools/`) and are gitignored. Nothing here stores your
  Workday password.
- The saved session is a live credential — treat `~/.config/workday-tools/` like
  an SSH key.
```
