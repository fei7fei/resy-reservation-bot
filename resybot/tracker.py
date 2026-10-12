"""Cancellation tracker: records when sold-out days open up at your profiles' restaurants,
and how fast those tables get booked.

Every poll it fetches each restaurant's calendar (one request covers every
bookable day) and compares each day's status with the last poll:

  sold-out -> available   an opening (someone cancelled, or the venue added tables)
  available -> sold-out   that opening is gone again (records how long it lasted)
  new date opens          the daily release (e.g. 9:00 AM, 29 days out)

Speed tracking:
  * Around each profile's release time (from 45s before until 15 min after) the
    calendar is checked every 5 seconds instead of every minute.
  * Whenever a day opens (release or cancellation), its individual time slots are
    checked every 5-10 seconds, recording when each time appears and when it's
    booked -- so you can see e.g. "6:00 PM gone 14s after 9:00".

Everything goes into data/openings.db (SQLite), which the app charts.

Usage (the app starts it for you):
    python -m resybot.tracker [--interval 60]

Limits: the calendar says whether a day has *any* table, and slot timing has
5-10 second resolution, so things that happen faster than that can be missed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import random
import sqlite3
import sys
import time

import requests

from . import paths, profiles
from .bot import keep_awake
from .client import ResyClient, ResyError

log = logging.getLogger("resy")

SCHEMA = """
CREATE TABLE IF NOT EXISTS venues   (venue_id INTEGER PRIMARY KEY, name TEXT, first_poll TEXT, last_poll TEXT);
CREATE TABLE IF NOT EXISTS status   (venue_id INTEGER, party INTEGER, day TEXT, status TEXT, since TEXT,
                                     PRIMARY KEY (venue_id, party, day));
CREATE TABLE IF NOT EXISTS openings (id INTEGER PRIMARY KEY AUTOINCREMENT, venue_id INTEGER, party INTEGER,
                                     day TEXT, kind TEXT, opened_at TEXT, closed_at TEXT, times TEXT);
CREATE INDEX IF NOT EXISTS openings_venue ON openings (venue_id, party, day);
-- one row per time slot seen during an opening: when it showed up and when it was booked
CREATE TABLE IF NOT EXISTS slots    (id INTEGER PRIMARY KEY AUTOINCREMENT, opening_id INTEGER, time TEXT,
                                     seen_at TEXT, gone_at TEXT);
CREATE INDEX IF NOT EXISTS slots_opening ON slots (opening_id);
"""

RELEASE_BEFORE = dt.timedelta(seconds=45)
RELEASE_AFTER = dt.timedelta(minutes=15)
FAST_POLL = 5          # seconds, during a release window
SLOT_POLL = 10         # seconds, while an opening is being watched outside a release window
WATCH_LIMIT = dt.timedelta(hours=3)  # stop timing slots of an opening after this long


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")  # the app reads while we write
    conn.executescript(SCHEMA)
    return conn


def targets() -> dict[tuple[int, int], dict]:
    """(venue_id, party size) -> {name, token, release} for every profile's restaurant."""
    out: dict[tuple[int, int], dict] = {}
    for c in (profiles.load(p) for p in profiles.list_profiles()):
        if not c.get("venue_id"):
            continue
        key = (int(c["venue_id"]), int(c.get("party_size", 2)))
        t = out.setdefault(key, {"name": c.get("restaurant_name", str(c["venue_id"])), "token": None, "release": None})
        t["token"] = t["token"] or c.get("auth_token")
        t["release"] = t["release"] or c.get("release_time") or "09:00:00"
    return out


def release_window(t: dict, now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    rel = dt.datetime.combine(now.date(), dt.datetime.strptime(t["release"], "%H:%M:%S").time())
    return rel - RELEASE_BEFORE, rel + RELEASE_AFTER


def in_release_window(t: dict, now: dt.datetime) -> bool:
    start, end = release_window(t, now)
    return start <= now <= end


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class Tracker:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.client = ResyClient()
        # opening_id -> {venue_id, party, day, token, until}
        self.watching: dict[int, dict] = {}

    # -- calendar ---------------------------------------------------------

    def poll(self, venue_id: int, party: int, target: dict):
        conn = self.conn
        cal = self.client.get_calendar(venue_id, party, days=90)
        ts = now_iso()
        now = dt.datetime.now()
        seen_before = conn.execute("SELECT 1 FROM status WHERE venue_id=? AND party=? LIMIT 1", (venue_id, party)).fetchone()
        conn.execute("INSERT INTO venues (venue_id, name, first_poll, last_poll) VALUES (?,?,?,?) "
                     "ON CONFLICT(venue_id) DO UPDATE SET name=excluded.name, last_poll=excluded.last_poll",
                     (venue_id, target["name"], ts, ts))
        today = dt.date.today().isoformat()
        days = [d for d in cal.get("scheduled", []) if d["date"] >= today]
        newest = max((d["date"] for d in days), default=None)
        releasing = in_release_window(target, now)
        for d in days:
            day = d["date"]
            status = (d.get("inventory") or {}).get("reservation", "unknown")
            row = conn.execute("SELECT status FROM status WHERE venue_id=? AND party=? AND day=?",
                               (venue_id, party, day)).fetchone()
            if row is None:
                conn.execute("INSERT INTO status VALUES (?,?,?,?,?)", (venue_id, party, day, status, ts))
                if seen_before and status == "available":
                    self.record_open(venue_id, party, day, "release", ts, target)
                continue
            prev = row[0]
            if prev == status:
                continue
            conn.execute("UPDATE status SET status=?, since=? WHERE venue_id=? AND party=? AND day=?",
                         (status, ts, venue_id, party, day))
            if status == "available":
                # A date that unlocks during the release window is the release, not a cancellation.
                kind = "release" if releasing and day == newest else "cancellation"
                self.record_open(venue_id, party, day, kind, ts, target)
            elif prev == "available":
                self.close_openings(venue_id, party, day, ts)
                log.info("%s: %s booked up again (%s)", target["name"], day, status)
        conn.commit()

    def close_openings(self, venue_id: int, party: int, day: str, ts: str):
        ids = [r[0] for r in self.conn.execute(
            "SELECT id FROM openings WHERE venue_id=? AND party=? AND day=? AND closed_at IS NULL", (venue_id, party, day))]
        for oid in ids:
            self.conn.execute("UPDATE openings SET closed_at=? WHERE id=?", (ts, oid))
            self.conn.execute("UPDATE slots SET gone_at=? WHERE opening_id=? AND gone_at IS NULL", (ts, oid))
            self.watching.pop(oid, None)

    def record_open(self, venue_id, party, day, kind, ts, target):
        cur = self.conn.execute("INSERT INTO openings (venue_id, party, day, kind, opened_at, times) VALUES (?,?,?,?,?,?)",
                                (venue_id, party, day, kind, ts, "[]"))
        oid = cur.lastrowid
        self.watching[oid] = {"venue_id": venue_id, "party": party, "day": day, "token": target.get("token"),
                              "until": dt.datetime.now() + WATCH_LIMIT, "name": target["name"]}
        times = self.check_slots(oid)
        log.info("%s: %s OPENED (%s, party of %s) %s", target["name"], day, kind, party, ", ".join(times or []))

    # -- per-slot timing ----------------------------------------------------

    def check_slots(self, oid: int) -> list[str] | None:
        """Record which times are bookable right now for a watched opening. None if it can't be checked."""
        w = self.watching.get(oid)
        if not w or not w["token"]:
            return None
        try:
            self.client.set_auth_token(w["token"])
            times = sorted({s.time_str for s in self.client.find_slots(w["venue_id"], w["day"], w["party"])})
        except (ResyError, requests.RequestException) as e:
            log.debug("couldn't fetch times for %s: %s", w["day"], e)
            return None
        ts = now_iso()
        open_rows = {t: sid for sid, t in self.conn.execute(
            "SELECT id, time FROM slots WHERE opening_id=? AND gone_at IS NULL", (oid,))}
        for t in times:
            if t not in open_rows:
                self.conn.execute("INSERT INTO slots (opening_id, time, seen_at) VALUES (?,?,?)", (oid, t, ts))
        for t, sid in open_rows.items():
            if t not in times:
                self.conn.execute("UPDATE slots SET gone_at=? WHERE id=?", (ts, sid))
                log.info("%s: %s %s booked", w["name"], w["day"], t)
        all_times = sorted({r[0] for r in self.conn.execute("SELECT time FROM slots WHERE opening_id=?", (oid,))})
        self.conn.execute("UPDATE openings SET times=? WHERE id=?", (json.dumps(all_times), oid))
        if not times:  # every time got booked: the opening is over even if the calendar hasn't caught up
            self.conn.execute("UPDATE openings SET closed_at=COALESCE(closed_at, ?) WHERE id=?", (ts, oid))
            self.watching.pop(oid, None)
        elif dt.datetime.now() > w["until"]:
            self.watching.pop(oid, None)  # still open after hours: stop timing it
        self.conn.commit()
        return times

    # -- main loop ----------------------------------------------------------

    def run(self, interval: int):
        last_calendar = 0.0
        while True:
            tg = targets()
            now = dt.datetime.now()
            fast = any(in_release_window(t, now) for t in tg.values())
            if fast or time.monotonic() - last_calendar >= interval:
                last_calendar = time.monotonic()
                if not tg:
                    log.info("No profile has a restaurant yet -- waiting.")
                for (venue_id, party), t in tg.items():
                    try:
                        self.poll(venue_id, party, t)
                    except (ResyError, requests.RequestException, KeyError, ValueError) as e:
                        log.warning("%s: poll failed: %s", t["name"], e)
            for oid in list(self.watching):
                self.check_slots(oid)

            # Sleep until the next thing worth doing: a fast tick, a slot check, the next
            # release window opening, or the regular calendar poll.
            if fast:
                wait = FAST_POLL
            elif self.watching:
                wait = SLOT_POLL
            else:
                wait = max(1.0, interval - (time.monotonic() - last_calendar)) + random.uniform(0, 3)
                starts = [release_window(t, now)[0] for t in tg.values()]
                starts = [s for s in starts + [s + dt.timedelta(days=1) for s in starts] if s > now]
                if starts:
                    wait = min(wait, max(1.0, (min(starts) - now).total_seconds()))
            time.sleep(wait)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval", type=int, default=60, help="seconds between calendar polls (min 30)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(sys.stdout)])
    keep_awake()
    interval = max(30, args.interval)
    os.makedirs(paths.DATA, exist_ok=True)
    conn = connect(paths.DB)
    log.info("Tracker started -- calendar every %ss (every %ss around release time), saving to %s",
             interval, FAST_POLL, paths.DB)
    Tracker(conn).run(interval)


if __name__ == "__main__":
    main()
