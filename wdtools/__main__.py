"""CLI entrypoint: `python -m wdtools <command>`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import auth, gcal, workday
from .config import Config


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "-c",
        "--config",
        default="config.toml",
        help="Path to config.toml (default: ./config.toml)",
    )


def cmd_login(args) -> int:
    cfg = Config.load(args.config)
    auth.ensure_session(cfg, force_login=args.force)
    print("Workday session ready.")
    return 0


def cmd_snapshot(args) -> int:
    cfg = Config.load(args.config)
    auth.ensure_session(cfg)
    out_dir = Path(args.out)
    with auth.workday_context(cfg, headless=True) as context:
        html_path = workday.snapshot(context, cfg, out_dir)
    print(f"Saved snapshot next to: {html_path}")
    print("Inspect it to wire up selectors in wdtools/workday.py.")
    return 0


def cmd_scrape(args) -> int:
    cfg = Config.load(args.config)
    auth.ensure_session(cfg)
    with auth.workday_context(cfg, headless=True) as context:
        absences = workday.scrape_absences(context, cfg)
    for ev in sorted(absences, key=lambda e: (e.start, e.person)):
        print(f"{ev.start}..{ev.end}  {ev.summary}")
    print(f"\n{len(absences)} absence(s).", file=sys.stderr)
    return 0


def cmd_sync(args) -> int:
    cfg = Config.load(args.config)
    auth.ensure_session(cfg)
    with auth.workday_context(cfg, headless=True) as context:
        absences = workday.scrape_absences(context, cfg)
    result = gcal.sync(cfg, absences, dry_run=args.dry_run)
    prefix = "[dry-run] " if args.dry_run else ""
    print(f"{prefix}Synced {len(absences)} absence(s): {result}")
    return 0


def cmd_tray(args) -> int:
    cfg = Config.load(args.config)
    from . import trayapp  # imported lazily so headless CLI use doesn't need pystray

    return trayapp.run(cfg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wdtools", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="Open a browser to log in and save the session")
    p_login.add_argument("--force", action="store_true", help="Re-login even if a session exists")
    _add_common(p_login)
    p_login.set_defaults(func=cmd_login)

    p_snap = sub.add_parser("snapshot", help="Dump the absence page for selector dev")
    p_snap.add_argument("--out", default="snapshots", help="Output directory")
    _add_common(p_snap)
    p_snap.set_defaults(func=cmd_snapshot)

    p_scrape = sub.add_parser("scrape", help="Scrape and print absences (no calendar writes)")
    _add_common(p_scrape)
    p_scrape.set_defaults(func=cmd_scrape)

    p_sync = sub.add_parser("sync", help="Scrape Workday and sync to Google Calendar")
    p_sync.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    _add_common(p_sync)
    p_sync.set_defaults(func=cmd_sync)

    p_tray = sub.add_parser("tray", help="Run the menu bar / tray app with a scheduler")
    _add_common(p_tray)
    p_tray.set_defaults(func=cmd_tray)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
