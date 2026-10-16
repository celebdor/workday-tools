"""Workday session handling.

Flow, matching the "log in once, then run in the background" model:

1. ``ensure_session`` checks for a saved, still-valid session on disk.
2. If none (or expired), it opens a *real, visible* browser at Workday so you can
   complete SSO/MFA by hand.
3. Once it detects you've landed past the login pages, it saves the session
   (cookies + storage) to disk and closes the visible browser.
4. Every later step — and every later run until the session expires — reuses that
   saved session headlessly, no browser popup.
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from .config import Config

logger = logging.getLogger(__name__)

# How long to wait for the human to finish logging in, in milliseconds.
LOGIN_TIMEOUT_MS = 5 * 60 * 1000


def _notify(message: str) -> None:
    """Surface a user-facing message to the log and, when possible, the terminal.

    Always logged so it lands in ``tray.log``. Printing to stderr is best-effort:
    the tray runs detached with no attached terminal, so writing to ``sys.stderr``
    there raises ``OSError`` — swallow it rather than aborting the login task.
    """
    logger.info(message)
    try:
        print(message, file=sys.stderr)
    except OSError:
        pass


def _launch_chromium(pw, *, headless: bool):
    """Launch Chromium, turning the "browser not installed" error into a hint."""
    try:
        return pw.chromium.launch(headless=headless)
    except PlaywrightError as exc:
        msg = str(exc)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            raise RuntimeError(
                "Chromium isn't installed for Playwright yet. Run "
                "`wdtools-playwright-install` once (or `uv run playwright install "
                "chromium` in a dev checkout)."
            ) from exc
        raise


def _looks_logged_in(page: Page, cfg: Config) -> bool:
    return cfg.login_success_url_substring in page.url


def interactive_login(cfg: Config) -> None:
    """Open a visible browser, let the user log in, and persist the session."""
    with sync_playwright() as pw:
        browser = _launch_chromium(pw, headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(cfg.workday_home_url, wait_until="domcontentloaded")

        _notify(
            "\n>>> A browser window has opened. Log in to Workday (SSO + MFA).\n"
            ">>> Once you can see your Workday home page, leave it — this will\n"
            ">>> detect the login, save the session, and close the window.\n"
        )

        # Poll for a logged-in URL rather than relying on a single navigation,
        # because SSO bounces through several redirects.
        page.wait_for_url(
            lambda url: cfg.login_success_url_substring in url,
            timeout=LOGIN_TIMEOUT_MS,
        )
        # Give the SPA a moment to settle before snapshotting storage.
        page.wait_for_load_state("networkidle")

        cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(cfg.state_path))
        _notify(f">>> Session saved to {cfg.state_path}")
        browser.close()


def _session_is_valid(cfg: Config) -> bool:
    """Open the saved session headlessly and confirm it still lands logged in."""
    if not Path(cfg.state_path).exists():
        logger.debug("No saved session file at %s — session invalid.", cfg.state_path)
        return False
    with sync_playwright() as pw:
        browser = _launch_chromium(pw, headless=True)
        try:
            context = browser.new_context(storage_state=str(cfg.state_path))
            page = context.new_page()
            page.goto(cfg.workday_home_url, wait_until="domcontentloaded")
            try:
                page.wait_for_url(
                    lambda url: cfg.login_success_url_substring in url,
                    timeout=15_000,
                )
                logger.debug("Session check: landed logged in (%s).", page.url)
                return True
            except Exception:
                logger.debug(
                    "Session check: did not reach a logged-in URL (at %s); "
                    "session is expired/invalid.", page.url,
                )
                return False
        finally:
            browser.close()


def ensure_session(cfg: Config, force_login: bool = False) -> None:
    """Guarantee a usable saved session exists, prompting interactive login if not."""
    if not force_login:
        logger.info("Checking saved Workday session…")
        if _session_is_valid(cfg):
            logger.info("Saved Workday session is valid; reusing it.")
            return
        logger.info("No valid saved session; interactive login required.")
    interactive_login(cfg)


@contextmanager
def workday_context(cfg: Config, headless: bool = True) -> Iterator[BrowserContext]:
    """Yield a browser context authenticated with the saved Workday session.

    Call ``ensure_session`` before this. Raises if no saved session exists.
    """
    if not Path(cfg.state_path).exists():
        raise RuntimeError("No saved Workday session. Run `login` (or `sync`) first.")
    with sync_playwright() as pw:
        browser = _launch_chromium(pw, headless=headless)
        try:
            context = browser.new_context(storage_state=str(cfg.state_path))
            yield context
        finally:
            browser.close()
