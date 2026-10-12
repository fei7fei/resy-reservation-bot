"""One-time move from the old layout (config.yaml, profiles/, .runs/, openings.db in the project folder)
into data/. Copies -- the old files are left in place as a backup.

    python -m resybot.migrate
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import shutil
import sqlite3

import yaml

from . import paths, profiles

DROP_KEYS = ("email", "password", "ui_theme")  # old placeholder login fields, retired desktop-app setting


def _clean(cfg: dict) -> dict:
    c = {k: v for k, v in cfg.items() if k not in DROP_KEYS}
    if "PASTE" in str(c.get("api_key", "")):
        c.pop("api_key")
    if int(c.get("watch_poll_interval_seconds", 15)) >= 20:
        c["watch_poll_interval_seconds"] = 15  # the calendar-based watch checks faster with fewer requests
    return c


def _bookings_from_log(log_path: str, json_path: str, slug: str, cfg: dict) -> list[dict]:
    """Recover real bookings from an old bot log ("*** Booked YYYY-MM-DD at HH:MM! ***")."""
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            day = dt.datetime.fromisoformat(json.load(f)["started"]).date()
    except (OSError, ValueError, KeyError):
        day = dt.date.today()
    out, prev = [], None
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = re.match(r"^(\d\d):(\d\d):(\d\d) ", line)
            if m:
                t = dt.time(int(m[1]), int(m[2]), int(m[3]))
                if prev and t < prev:
                    day += dt.timedelta(days=1)
                prev = t
            b = re.search(r"\*\*\* Booked (\d{4}-\d\d-\d\d) at (\d\d:\d\d)! \*\*\*", line)
            if b and m:
                out.append({"profile": slug, "who": cfg.get("profile_name"), "restaurant": cfg.get("restaurant_name"),
                            "venue_id": cfg.get("venue_id"), "party": cfg.get("party_size"), "day": b[1], "time": b[2],
                            "reservation_id": None, "booked_at": dt.datetime.combine(day, t).isoformat()})
    return out


def migrate(root: str = paths.ROOT) -> list[str]:
    done = []
    os.makedirs(paths.PROFILES, exist_ok=True)
    os.makedirs(paths.TRACKER_RUN, exist_ok=True)
    found: list[tuple[str, dict, str]] = []  # (slug, cfg, old run dir)

    old_main = os.path.join(root, "config.yaml")
    if os.path.exists(old_main):
        with open(old_main, "r", encoding="utf-8") as f:
            cfg = _clean(yaml.safe_load(f) or {})
        cfg.setdefault("profile_name", "Me")
        found.append((profiles.MAIN, cfg, os.path.join(root, ".runs")))
    for p in sorted(glob.glob(os.path.join(root, "profiles", "*.yaml"))):
        slug = os.path.splitext(os.path.basename(p))[0]
        with open(p, "r", encoding="utf-8") as f:
            found.append((slug, _clean(yaml.safe_load(f) or {}), os.path.join(root, "profiles", f"{slug}.runs")))

    booked = []
    for slug, cfg, old_runs in found:
        if os.path.exists(paths.profile_file(slug)):
            done.append(f"kept existing data/profiles/{slug}.yaml")
        else:
            profiles.save(slug, cfg)
            done.append(f"profile {slug} ({cfg.get('profile_name')})")
        for mode in ("watch", "snipe"):
            log = os.path.join(old_runs, f"{mode}.log")
            if os.path.exists(log):
                booked += _bookings_from_log(log, os.path.join(old_runs, f"{mode}.json"), slug, cfg)

    bfile = os.path.join(paths.DATA, "bookings.jsonl")
    if booked and not os.path.exists(bfile):
        with open(bfile, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(b) + "\n" for b in booked)
        done.append(f"{len(booked)} booking(s) recovered from old logs")

    old_db = os.path.join(root, "openings.db")
    if os.path.exists(old_db) and not os.path.exists(paths.DB):
        src = sqlite3.connect(old_db)
        dst = sqlite3.connect(paths.DB)
        src.backup(dst)  # consistent copy, including anything still in the WAL file
        dst.close()
        src.close()
        done.append("tracker database")
    old_tlog = os.path.join(root, ".runs", "tracker.log")
    if os.path.exists(old_tlog) and not os.path.exists(os.path.join(paths.TRACKER_RUN, "tracker.log")):
        shutil.copy2(old_tlog, os.path.join(paths.TRACKER_RUN, "tracker.log"))
        done.append("tracker log")
    return done


if __name__ == "__main__":
    for line in migrate():
        print("migrated:", line)
