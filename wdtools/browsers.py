"""One-shot helper to download the Chromium build Playwright drives.

Exposed as the ``wdtools-playwright-install`` console script. People who install
the tool with ``uv tool install`` only get *our* entry points linked onto PATH,
not Playwright's own ``playwright`` CLI — so this wrapper is the one-step way for
them to fetch the browser Playwright needs.
"""

from __future__ import annotations

import subprocess
import sys


def install() -> int:
    """Run ``playwright install chromium`` with the current interpreter."""
    return subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"])


if __name__ == "__main__":
    raise SystemExit(install())
