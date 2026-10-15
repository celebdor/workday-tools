"""Menu bar / system tray app (Linux + macOS) that keeps the sync running and
shows session/sync status through the tray icon's color.

Design notes
------------
* All Workday/Google work runs on a single dedicated worker thread, fed by a
  queue. That serializes operations (no overlapping syncs) and keeps every
  Playwright sync-API object on one thread, which it requires.
* The scheduler thread just enqueues a sync when a configured time arrives.
* pystray owns the main thread (``icon.run()``); we mutate ``icon.icon`` and the
  menu text from the worker to reflect state.
"""

from __future__ import annotations

import enum
import logging
import queue
import threading
from datetime import datetime

import pystray
from PIL import Image, ImageDraw

from . import auth, gcal, workday
from .config import Config

logger = logging.getLogger(__name__)


class State(enum.Enum):
    UNKNOWN = "Starting…"
    NEEDS_LOGIN = "Needs login"
    IDLE = "Logged in"
    SYNCING = "Syncing…"
    ERROR = "Error"


_COLORS = {
    State.UNKNOWN: (150, 150, 150),
    State.NEEDS_LOGIN: (230, 160, 40),
    State.IDLE: (60, 175, 90),
    State.SYNCING: (70, 130, 220),
    State.ERROR: (205, 60, 60),
}

# How often to re-check the saved session so the icon reflects an expiry that
# happens while the app is just sitting idle.
SESSION_RECHECK_SECONDS = 30 * 60


def _make_icon(color: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=color + (255,), outline=(255, 255, 255, 210), width=4)
    # A small "W" so it reads as Workday even for color-blind users.
    d.text((22, 20), "W", fill=(255, 255, 255, 255))
    return img


class TrayApp:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.state = State.UNKNOWN
        self.detail = ""
        self.last_sync: datetime | None = None
        self.last_result: str = ""

        self._tasks: "queue.Queue" = queue.Queue()
        self._stop = threading.Event()
        self._icon = pystray.Icon(
            "workday-sync",
            icon=_make_icon(_COLORS[State.UNKNOWN]),
            title="Workday Sync",
            menu=self._menu(),
        )

    # ---- UI ---------------------------------------------------------------

    def _status_text(self, _item=None) -> str:
        line = self.state.value
        if self.detail:
            line += f" — {self.detail}"
        return line

    def _last_sync_text(self, _item=None) -> str:
        if not self.last_sync:
            return "Last sync: never"
        when = self.last_sync.strftime("%a %H:%M")
        return f"Last sync: {when}  {self.last_result}".rstrip()

    def _menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(self._status_text, None, enabled=False),
            pystray.MenuItem(self._last_sync_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Sync now", lambda: self._enqueue(self._task_sync)),
            pystray.MenuItem("Log in to Workday…", lambda: self._enqueue(self._task_login)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )

    def _set_state(self, state: State, detail: str = "") -> None:
        logger.info("State -> %s%s", state.name, f" ({detail})" if detail else "")
        self.state = state
        self.detail = detail
        self._icon.icon = _make_icon(_COLORS[state])
        self._icon.update_menu()

    # ---- tasks (run on the worker thread) --------------------------------

    def _task_check_session(self) -> None:
        logger.info("Checking saved Workday session…")
        if auth._session_is_valid(self.cfg):
            self._set_state(State.IDLE)
        else:
            self._set_state(
                State.NEEDS_LOGIN,
                "click the tray → ‘Log in to Workday…’",
            )

    def _task_login(self) -> None:
        logger.info("Interactive Workday login requested; opening browser.")
        self._set_state(State.NEEDS_LOGIN, "opening browser…")
        auth.interactive_login(self.cfg)
        self._task_check_session()

    def _task_sync(self) -> None:
        # Never pop a browser unattended here — that's what the "Log in" item is for.
        logger.info("Sync requested.")
        # If we already know we're logged out, skip cheaply: don't launch a headless
        # browser just to re-confirm (that would block the queue behind each sync).
        if self.state == State.NEEDS_LOGIN:
            logger.info("Sync skipped: not logged in (use ‘Log in to Workday…’).")
            return
        if not auth._session_is_valid(self.cfg):
            logger.info("Sync skipped: session expired; needs manual login.")
            self._set_state(State.NEEDS_LOGIN, "log in to enable syncing")
            return
        self._set_state(State.SYNCING)
        with auth.workday_context(self.cfg, headless=True) as context:
            absences = workday.scrape_absences(context, self.cfg)
        result = gcal.sync(self.cfg, absences)
        self.last_sync = datetime.now()
        self.last_result = f"({result})"
        self._set_state(State.IDLE)

    # ---- threads ----------------------------------------------------------

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                task = self._tasks.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                task()
            except Exception as exc:  # keep the app alive; surface via the icon
                logger.exception("Tray task failed: %s", task.__name__)
                self._set_state(State.ERROR, str(exc).splitlines()[0][:80])
            finally:
                self._tasks.task_done()

    def _scheduler(self) -> None:
        def hhmm(t: str) -> tuple[int, int]:
            hh, mm = t.split(":")
            return int(hh), int(mm)

        # Slots already past when we start today: mark them fired so we don't fire
        # one retroactive sync *per* past slot — do a single startup catch-up.
        now = datetime.now()
        today = now.date().isoformat()
        fired: set[tuple[str, str]] = {
            (today, t) for t in self.cfg.sync_times if (now.hour, now.minute) >= hhmm(t)
        }
        if fired:
            logger.info("Startup catch-up: %d past slot(s) today; enqueuing one sync.", len(fired))
            self._enqueue(self._task_sync)

        last_session_check = datetime.now()  # a check is already enqueued at launch
        while not self._stop.wait(20):
            now = datetime.now()
            today = now.date().isoformat()
            fired = {slot for slot in fired if slot[0] == today}  # prune old days
            for t in self.cfg.sync_times:
                slot = (today, t)
                if (now.hour, now.minute) >= hhmm(t) and slot not in fired:
                    fired.add(slot)
                    logger.info("Scheduled sync time %s reached; enqueuing sync.", t)
                    self._enqueue(self._task_sync)

            # Periodically re-check the saved session so an expiry while idle turns
            # the icon amber on its own. Skip while syncing or showing an error so
            # we don't clobber that state.
            if (now - last_session_check).total_seconds() >= SESSION_RECHECK_SECONDS:
                last_session_check = now
                if self.state in (State.IDLE, State.NEEDS_LOGIN):
                    logger.debug("Periodic session recheck due; enqueuing check.")
                    self._enqueue(self._task_check_session)

    # ---- lifecycle --------------------------------------------------------

    def _enqueue(self, task) -> None:
        self._tasks.put(task)

    def _quit(self) -> None:
        self._stop.set()
        self._icon.stop()

    def run(self) -> None:
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._scheduler, daemon=True).start()
        self._enqueue(self._task_check_session)  # reflect session state at launch
        self._icon.run()  # blocks on the main thread until Quit


def run(cfg: Config) -> int:
    TrayApp(cfg).run()
    return 0
