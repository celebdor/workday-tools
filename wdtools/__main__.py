"""CLI entrypoint: `wdtools <command>` (or `python -m wdtools <command>`)."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import auth, config as config_mod, gcal, qc, workday
from .config import Config


def _add_common(p: argparse.ArgumentParser) -> None:
    default = config_mod.default_config_path()
    p.add_argument(
        "-c",
        "--config",
        default=str(default),
        help=f"Path to config.toml (default: {default})",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Show progress logging (-v for info, -vv for debug).",
    )


def _add_qc_cache_args(p: argparse.ArgumentParser) -> None:
    """Caching flags shared by qc-scrape and qc-sync."""
    p.add_argument(
        "--refresh",
        action="store_true",
        help="Re-scrape every task from Workday, ignoring the cache (and rewrite it)",
    )
    p.add_argument(
        "--offline",
        action="store_true",
        help="Don't contact Workday at all; use only cached connections",
    )


def _console_level(verbosity: int) -> int:
    if verbosity >= 2:
        return logging.DEBUG
    if verbosity == 1:
        return logging.INFO
    return logging.WARNING


def _configure_logging(verbosity: int) -> None:
    # Root passes everything; each handler filters to its own level, so a later
    # file handler can be more verbose than the console (used by the tray).
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(_console_level(verbosity))
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    root.handlers.clear()
    root.addHandler(console)
    # Quiet noisy third parties unless we're at -vv (logger-level filter, so it
    # applies to every handler including the file one).
    if verbosity < 2:
        for noisy in ("googleapiclient", "google", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def _add_file_logging(verbosity: int) -> Path:
    """Attach a rotating file handler (for the long-running tray). Returns its path."""
    from logging.handlers import RotatingFileHandler

    # Keep a useful operational log even without -v: floor the file at INFO.
    level = logging.DEBUG if verbosity >= 2 else logging.INFO
    directory = config_mod.log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "tray.log"
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.getLogger().addHandler(handler)
    return path


def cmd_init(args) -> int:
    dest = Path(args.config)
    if dest.exists() and not args.force:
        print(
            f"{dest} already exists. Use --force to overwrite it.",
            file=sys.stderr,
        )
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(config_mod.CONFIG_TEMPLATE, encoding="utf-8")
    print(f"Wrote config template to {dest}")
    print("Edit it to fill in your Workday URLs and Google calendar ID, then run `wdtools login`.")
    return 0


def cmd_login(args) -> int:
    cfg = Config.load(args.config)
    auth.ensure_session(cfg, force_login=args.force)
    print("Workday session ready.")
    return 0


def cmd_setup_google(args) -> int:
    cfg = Config.load(args.config)
    from . import google_setup  # lazy: only setup needs gcloud/webbrowser

    return google_setup.run(cfg)


def cmd_snapshot(args) -> int:
    cfg = Config.load(args.config)
    auth.ensure_session(cfg)
    out_dir = Path(args.out)
    with auth.workday_context(cfg, headless=True) as context:
        html_path = workday.snapshot(
            context, cfg, out_dir, open_view_menu=args.open_view_menu
        )
    print(f"Saved snapshot next to: {html_path}")
    print(
        f"Captured XHR/JSON responses are in {out_dir}/ — see the "
        "'*-xhr-index.json' manifest. Prefer those over the HTML for wiring up "
        "wdtools/workday.py:_parse_events."
    )
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


def cmd_qc_snapshot(args) -> int:
    cfg = Config.load(args.config)
    auth.ensure_session(cfg)
    out_dir = Path(args.out)
    with auth.workday_context(cfg, headless=not args.headful) as context:
        index_path = qc.snapshot(context, cfg, out_dir, task_index=args.task)
    print(f"Snapshot written under {out_dir}/ (XHR manifest: {index_path}).")
    print(
        "Inspect the qc-*.html / *.png and the '*-xhr-index.json' manifest, then "
        "finalize the selectors in wdtools/qc.py:SELECTORS."
    )
    return 0


def _qc_connections(args, cfg):
    """Resolve connections for qc-scrape/qc-sync, honoring --offline / --refresh."""
    if args.offline:
        connections = qc.load_offline_connections(cfg)
        if not connections:
            raise RuntimeError(
                "No cached connections to work from. Run without --offline once "
                "(optionally with --refresh) to populate the cache."
            )
        print(f"Using {len(connections)} cached connection(s) (offline).", file=sys.stderr)
        return connections
    auth.ensure_session(cfg)
    with auth.workday_context(cfg, headless=not args.headful) as context:
        return qc.scrape_quarterly_connections(context, cfg, refresh=args.refresh)


def cmd_qc_scrape(args) -> int:
    cfg = Config.load(args.config)
    cfg.validate_talent()
    connections = _qc_connections(args, cfg)
    for c in connections:
        print(f"\n# {c.employee}  [{c.tab_name}]  (period: {c.period_label!r})")
        if c.previous is not None:
            mgr = "yes" if c.previous.manager_html.strip() else "no"
            assoc = "yes" if c.previous.associate_html.strip() else "no"
            print(f"  (previous {c.previous.label}: manager={mgr}, associate={assoc})")
        else:
            print("  (previous priorities: none found)")
        for section in c.sections:
            print(f"  ## {section.name}")
            for q in section.questions:
                print(f"    - Q: {q.question}")
                print(f"      A: {q.answer}")
    print(f"\n{len(connections)} quarterly connection(s).", file=sys.stderr)
    return 0


def cmd_qc_sync(args) -> int:
    cfg = Config.load(args.config)
    cfg.validate_talent()
    from . import gdocs  # lazy: only this command needs the Docs client

    connections = _qc_connections(args, cfg)
    result = gdocs.sync(cfg, connections, dry_run=args.dry_run, replace=args.replace)
    prefix = "[dry-run] " if args.dry_run else ""
    print(f"{prefix}Processed {len(connections)} connection(s): {result}")
    return 0


def cmd_tray(args) -> int:
    cfg = Config.load(args.config)
    from . import trayapp  # imported lazily so headless CLI use doesn't need pystray

    log_path = _add_file_logging(args.verbose)
    print(f"Logging to {log_path}")
    logging.getLogger(__name__).info("Tray starting; logging to %s", log_path)
    return trayapp.run(cfg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wdtools", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Write a config.toml template to the default location")
    p_init.add_argument("--force", action="store_true", help="Overwrite an existing config.toml")
    _add_common(p_init)
    p_init.set_defaults(func=cmd_init)

    p_login = sub.add_parser("login", help="Open a browser to log in and save the session")
    p_login.add_argument("--force", action="store_true", help="Re-login even if a session exists")
    _add_common(p_login)
    p_login.set_defaults(func=cmd_login)

    p_gsetup = sub.add_parser(
        "setup-google",
        help="Set up Google (project, Calendar API, OAuth client, authorization)",
    )
    _add_common(p_gsetup)
    p_gsetup.set_defaults(func=cmd_setup_google)

    p_snap = sub.add_parser("snapshot", help="Dump the absence page for selector dev")
    p_snap.add_argument("--out", default="snapshots", help="Output directory")
    p_snap.add_argument(
        "--open-view-menu",
        action="store_true",
        help="Open the Day/Week/Month interval menu before dumping (for selector dev)",
    )
    _add_common(p_snap)
    p_snap.set_defaults(func=cmd_snapshot)

    p_scrape = sub.add_parser("scrape", help="Scrape and print absences (no calendar writes)")
    _add_common(p_scrape)
    p_scrape.set_defaults(func=cmd_scrape)

    p_sync = sub.add_parser("sync", help="Scrape Workday and sync to Google Calendar")
    p_sync.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    _add_common(p_sync)
    p_sync.set_defaults(func=cmd_sync)

    p_qc_snap = sub.add_parser(
        "qc-snapshot", help="Dump the My Tasks / Quarterly Connection pages for selector dev"
    )
    p_qc_snap.add_argument("--out", default="snapshots", help="Output directory")
    p_qc_snap.add_argument(
        "--task", type=int, default=None,
        help="1-based index of a Quarterly Connection task to open and snapshot in full",
    )
    p_qc_snap.add_argument(
        "--headful", action="store_true", help="Run with a visible browser (debugging)"
    )
    _add_common(p_qc_snap)
    p_qc_snap.set_defaults(func=cmd_qc_snapshot)

    p_qc_scrape = sub.add_parser(
        "qc-scrape", help="Scrape Quarterly Connections and print them (no doc writes)"
    )
    _add_qc_cache_args(p_qc_scrape)
    p_qc_scrape.add_argument(
        "--headful", action="store_true", help="Run with a visible browser (debugging)"
    )
    _add_common(p_qc_scrape)
    p_qc_scrape.set_defaults(func=cmd_qc_scrape)

    p_qc_sync = sub.add_parser(
        "qc-sync", help="Scrape Quarterly Connections and write them into the Google Doc"
    )
    p_qc_sync.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    p_qc_sync.add_argument(
        "--replace",
        action="store_true",
        help="Rewrite employees already in the tab (deletes their existing block, "
        "including any manager response typed there)",
    )
    _add_qc_cache_args(p_qc_sync)
    p_qc_sync.add_argument(
        "--headful", action="store_true", help="Run with a visible browser (debugging)"
    )
    _add_common(p_qc_sync)
    p_qc_sync.set_defaults(func=cmd_qc_sync)

    p_tray = sub.add_parser("tray", help="Run the menu bar / tray app with a scheduler")
    _add_common(p_tray)
    p_tray.set_defaults(func=cmd_tray)

    args = parser.parse_args(argv)
    _configure_logging(getattr(args, "verbose", 0))
    try:
        return args.func(args)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
