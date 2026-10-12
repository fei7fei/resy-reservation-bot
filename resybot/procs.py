"""Start, stop and check the background processes: each profile's watch/snipe bot and the tracker.

Each run leaves three files in its run folder:
    <mode>.yaml   the settings snapshot the bot runs with (editing the profile doesn't change a running bot)
    <mode>.log    everything the bot prints
    <mode>.json   pid, start time, dates, live/dry -- used to find the process again later
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys

import psutil
import yaml

from . import paths

MODES = ("watch", "snipe")
_FLAGS = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0


def python_exe() -> str:
    """python.exe, not pythonw.exe -- the bots need a real stdout to write their logs."""
    exe = sys.executable
    alt = os.path.join(os.path.dirname(exe), "python.exe")
    return alt if os.path.basename(exe).lower() == "pythonw.exe" and os.path.exists(alt) else exe


def _read_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _alive(info: dict | None, *needles: str) -> dict | None:
    """info if its pid is still our process (pids get reused, so check the command line too)."""
    if not info:
        return None
    try:
        proc = psutil.Process(info["pid"])
        cmd = " ".join(proc.cmdline())
        if proc.is_running() and all(n in cmd for n in needles):
            return info
    except (psutil.Error, KeyError):
        pass
    return None


def _spawn(args: list[str], log_path: str, append: bool = False) -> int:
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a" if append else "w", encoding="utf-8") as log:
        proc = subprocess.Popen([python_exe(), "-u", "-m", *args], cwd=paths.ROOT, stdout=log,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=_FLAGS,
                                env={**os.environ, "RESY_DATA": paths.DATA})
    return proc.pid


def _terminate(info: dict | None):
    if info:
        try:
            psutil.Process(info["pid"]).terminate()
        except psutil.Error:
            pass


# -- bots ---------------------------------------------------------------------

def log_path(slug: str, mode: str) -> str:
    return os.path.join(paths.run_dir(slug), f"{mode}.log")


def run_info(slug: str, mode: str) -> dict | None:
    """The last run's info, running or not."""
    return _read_json(os.path.join(paths.run_dir(slug), f"{mode}.json"))


def bot_status(slug: str, mode: str) -> dict | None:
    """Run info if this profile's `mode` bot is running right now."""
    return _alive(run_info(slug, mode), "resybot.bot", f" {mode} ", os.path.join(paths.run_dir(slug), ""))


def start_bot(slug: str, mode: str, cfg: dict):
    rd = paths.run_dir(slug)
    os.makedirs(rd, exist_ok=True)
    snapshot = os.path.join(rd, f"{mode}.yaml")
    with open(snapshot, "w", encoding="utf-8") as f:
        yaml.safe_dump({**cfg, "profile_slug": slug}, f, sort_keys=False, allow_unicode=True)
    pid = _spawn(["resybot.bot", mode, "--config", snapshot, "-v"], log_path(slug, mode))
    with open(os.path.join(rd, f"{mode}.json"), "w", encoding="utf-8") as f:
        json.dump({"pid": pid, "started": dt.datetime.now().isoformat(timespec="seconds"),
                   "dates": cfg.get("watch_dates") or [], "live": not cfg.get("dry_run", True),
                   "restaurant": cfg.get("restaurant_name", "")}, f)


def stop_bot(slug: str, mode: str):
    _terminate(bot_status(slug, mode))


# -- tracker --------------------------------------------------------------------

TRACKER_LOG = os.path.join(paths.TRACKER_RUN, "tracker.log")


def tracker_status() -> dict | None:
    return _alive(_read_json(os.path.join(paths.TRACKER_RUN, "tracker.json")), "resybot.tracker")


def start_tracker(interval: int = 60):
    pid = _spawn(["resybot.tracker", "--interval", str(interval)], TRACKER_LOG, append=True)
    with open(os.path.join(paths.TRACKER_RUN, "tracker.json"), "w", encoding="utf-8") as f:
        json.dump({"pid": pid, "started": dt.datetime.now().isoformat(timespec="seconds")}, f)


def stop_tracker():
    _terminate(tracker_status())
