"""Reads bot logs and the tracker database into stats and a combined activity feed."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3

from . import paths, procs

LINE_RE = re.compile(r"^(\d\d):(\d\d):(\d\d) \[(\w+)\] (.*)$")
BOOKED_RE = re.compile(r"\*\*\* (?:Booked (\d{4}-\d\d-\d\d) at (\d\d:\d\d)!|DRY RUN match: (\d{4}-\d\d-\d\d) (\d\d:\d\d)) \*\*\*")
SPOTTED_RE = re.compile(r"Opening spotted on (\d{4}-\d\d-\d\d): \[(.*)\]")
CHECK_MARKERS = ('"POST /4/find', '"GET /4/venue/calendar')


def log_lines(slug: str, mode: str):
    """(datetime, level, message) for each line of a bot's latest log. Lines carry only HH:MM:SS,
    so dates are counted forward from the run's start time."""
    meta = procs.run_info(slug, mode)
    path = procs.log_path(slug, mode)
    if not meta or not os.path.exists(path):
        return
    day = dt.datetime.fromisoformat(meta["started"]).date()
    prev = None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LINE_RE.match(line.rstrip("\n"))
            if not m:
                continue
            t = dt.time(int(m[1]), int(m[2]), int(m[3]))
            if prev and t < prev:
                day += dt.timedelta(days=1)  # rolled past midnight
            prev = t
            yield dt.datetime.combine(day, t), m[4], m[5]


def run_stats(slug: str, mode: str) -> dict:
    """Checks, openings, bookings and problems from a bot's latest run."""
    out = {"checks": [], "openings": 0, "booked": [], "errors": 0, "token_expired": False}
    for when, level, msg in log_lines(slug, mode):
        if any(k in msg for k in CHECK_MARKERS):
            out["checks"].append(when)
        elif msg.startswith("Opening spotted") or msg.startswith("Found "):  # "Found" = older bot versions
            out["openings"] += 1
        elif msg.startswith("TOKEN EXPIRED"):
            out["token_expired"] = True
        elif level in ("WARNING", "ERROR"):
            out["errors"] += 1
        b = BOOKED_RE.search(msg)
        if b:
            out["booked"].append({"day": b[1] or b[3], "time": b[2] or b[4], "dry": bool(b[3])})
    return out


def profile_stats(slug: str) -> dict:
    a, b = run_stats(slug, "watch"), run_stats(slug, "snipe")
    return {"checks": sorted(a["checks"] + b["checks"]), "openings": a["openings"] + b["openings"],
            "booked": a["booked"] + b["booked"], "errors": a["errors"] + b["errors"],
            "token_expired": a["token_expired"] or b["token_expired"]}


def bot_events(slug: str, who: str) -> list[dict]:
    """Notable moments from a profile's bots."""
    out = []
    for mode in procs.MODES:
        for when, level, msg in log_lines(slug, mode):
            ev = None
            if b := BOOKED_RE.search(msg):
                dry = bool(b[3])
                ev = ("🧪" if dry else "🎉", f"{who} {'would book' if dry else 'booked'} {b[1] or b[3]} at {b[2] or b[4]}",
                      "dry" if dry else "booked")
            elif s := SPOTTED_RE.search(msg):
                ev = ("👀", f"{who}'s {mode} bot spotted {s[1]}: {s[2].replace(chr(39), '')}", "opening")
            elif msg.startswith("TOKEN EXPIRED"):
                ev = ("🔑", f"{who}'s token expired — {mode} bot stopped", "problem")
            elif msg.startswith("Timed out"):
                ev = ("⏱", f"{who}'s snipe missed: {msg}", "problem")
            elif msg.startswith("Bot crashed"):
                ev = ("💥", f"{who}'s {mode} bot crashed", "problem")
            elif msg.startswith("Signed in to Resy"):
                ev = ("▶", f"{who}'s {mode} bot started", "start")
            if ev:
                out.append({"when": when, "icon": ev[0], "text": ev[1], "kind": ev[2]})
    return out


def tracker_events(limit: int = 50) -> list[dict]:
    """Openings and release sell-outs recorded by the tracker."""
    if not os.path.exists(paths.DB):
        return []
    out = []
    try:
        with sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True, timeout=5) as conn:
            names = dict(conn.execute("SELECT venue_id, name FROM venues"))
            rows = conn.execute("SELECT venue_id, day, kind, opened_at, closed_at, times FROM openings "
                                "ORDER BY opened_at DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.Error:
        return []
    for venue_id, day, kind, opened, closed, times in rows:
        place = names.get(venue_id, venue_id)
        t = ", ".join(json.loads(times or "[]")) or "?"
        if kind == "release":
            out.append({"when": dt.datetime.fromisoformat(opened), "icon": "🗓",
                        "text": f"{place} released {day} ({t})", "kind": "tracker"})
            if closed:
                secs = (dt.datetime.fromisoformat(closed) - dt.datetime.fromisoformat(opened)).total_seconds()
                out.append({"when": dt.datetime.fromisoformat(closed), "icon": "🔥",
                            "text": f"{place} {day} sold out {int(secs)}s after release", "kind": "tracker"})
        else:
            out.append({"when": dt.datetime.fromisoformat(opened), "icon": "🔓",
                        "text": f"Cancellation at {place}: {day} ({t})", "kind": "tracker"})
    return out
