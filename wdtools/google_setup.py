"""Drive the Google side of setup: project, Calendar API, OAuth client, consent.

Google exposes no API to create a classic OAuth 2.0 *Desktop app* client ID, so
that one step (and downloading its JSON) stays manual in the Cloud Console. This
module automates everything around it with ``gcloud`` and the installed-app
consent flow, and walks the user through the manual bit.

Run interactively via ``wdtools setup-google``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from . import gcal
from .config import Config

CALENDAR_API = "calendar-json.googleapis.com"
DOCS_API = "docs.googleapis.com"  # for the Quarterly Connections (`qc-*`) commands


def _run(args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    """Run a gcloud command, surfacing a clean error on failure."""
    try:
        return subprocess.run(
            args,
            check=True,
            text=True,
            capture_output=capture,
        )
    except FileNotFoundError:
        raise RuntimeError("gcloud not found on PATH. Install the Google Cloud SDK.")
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"`{' '.join(args)}` failed:\n{detail}")


def _gcloud_out(args: list[str]) -> str:
    return _run(args, capture=True).stdout.strip()


def _require_gcloud() -> None:
    if shutil.which("gcloud") is None:
        raise RuntimeError(
            "gcloud not found on PATH. Install the Google Cloud SDK, then re-run."
        )


def _active_account() -> str:
    account = _gcloud_out(
        ["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"]
    )
    if not account:
        raise RuntimeError("No active gcloud account. Run `gcloud auth login` first.")
    return account


def _prompt(text: str) -> str:
    return input(text).strip()


def _choose_project() -> str:
    current = _gcloud_out(["gcloud", "config", "get-value", "project"])
    if current in ("", "(unset)"):
        current = ""
    if current:
        answer = _prompt(
            f"Use current gcloud project '{current}'? "
            "[Y]es / enter a different project id: "
        )
        if answer.lower() in ("", "y", "yes"):
            return current
        project = answer
    else:
        project = _prompt("Enter the Google Cloud project id to use (or create): ")
    if not project:
        raise RuntimeError("A project id is required.")

    exists = (
        subprocess.run(
            ["gcloud", "projects", "describe", project, "--format=value(projectId)"],
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )
    if not exists:
        if _prompt(f"Project '{project}' not found. Create it? [y/N]: ").lower() in (
            "y",
            "yes",
        ):
            _run(["gcloud", "projects", "create", project])
        else:
            raise RuntimeError(f"Project '{project}' does not exist.")
    return project


def _enable_apis(project: str) -> None:
    # Calendar (absence sync) and Docs (Quarterly Connections) — enabling both is
    # cheap, so a later `qc-sync` works without re-running setup. The Docs OAuth
    # consent itself runs on demand the first time `qc-sync` needs it.
    print(f"Enabling {CALENDAR_API} and {DOCS_API} on {project} …")
    _run(["gcloud", "services", "enable", CALENDAR_API, DOCS_API, f"--project={project}"])


def _open(url: str) -> None:
    print(f"  {url}")
    try:
        webbrowser.open(url)
    except Exception:
        pass


def _looks_like_client_json(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(data, dict) and bool(data.keys() & {"installed", "web"})


def _ensure_client_json(cfg: Config, project: str) -> None:
    dest = Path(cfg.google_credentials_path)
    if dest.exists() and _looks_like_client_json(dest):
        answer = _prompt(f"OAuth client already at {dest}. Reuse it? [Y/n]: ")
        if answer.lower() in ("", "y", "yes"):
            return

    print(
        "\nGoogle has no API to create a Desktop OAuth client, so do this once in\n"
        "the Console (both pages are opening in your browser):\n"
        "  1. Configure the OAuth consent screen. For a Workspace/organization\n"
        "     account (your corporate domain) pick User type 'Internal' — no test users\n"
        "     or app verification needed. For a personal Gmail pick 'External' and\n"
        "     add your own account under 'Test users'."
    )
    _open(f"https://console.cloud.google.com/apis/credentials/consent?project={project}")
    print(
        "  2. Create credentials → 'OAuth client ID' → Application type "
        "'Desktop app', then Download JSON."
    )
    _open(f"https://console.cloud.google.com/apis/credentials/oauthclient?project={project}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    while True:
        raw = _prompt(
            f"\nPath to the downloaded client JSON "
            f"(or press Enter once you've saved it to {dest}): "
        )
        src = Path(raw).expanduser() if raw else dest
        if not src.exists():
            print(f"  {src} not found — try again.")
            continue
        if not _looks_like_client_json(src):
            print(f"  {src} doesn't look like an OAuth client JSON — try again.")
            continue
        if src != dest:
            dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"  Saved OAuth client to {dest}")
        return


def run(cfg: Config) -> int:
    _require_gcloud()
    print(f"gcloud account: {_active_account()}")
    project = _choose_project()
    _run(["gcloud", "config", "set", "project", project])
    _enable_apis(project)
    _ensure_client_json(cfg, project)

    print("\nAuthorizing — a browser window will open for Google consent …")
    gcal.authorize(cfg)  # runs the installed-app flow, writes token.json
    print(gcal.check_calendar_access(cfg))
    print("\nGoogle setup complete. You can now run `wdtools sync`.")
    print(
        "For Quarterly Connections (`wdtools qc-sync`), the Docs API is enabled; a "
        "one-time Google consent for Docs runs the first time you use it."
    )
    return 0
