"""Permanent record of real bookings (data/bookings.jsonl), so they survive bot restarts."""
from __future__ import annotations

import datetime as dt
import json
import os

from . import paths

FILE = os.path.join(paths.DATA, "bookings.jsonl")


def record(cfg: dict, day: str, hhmm: str, reservation_id=None):
    os.makedirs(paths.DATA, exist_ok=True)
    row = {"profile": cfg.get("profile_slug"), "who": cfg.get("profile_name"), "restaurant": cfg.get("restaurant_name"),
           "venue_id": cfg.get("venue_id"), "party": cfg.get("party_size"), "day": day, "time": hhmm,
           "reservation_id": reservation_id, "booked_at": dt.datetime.now().isoformat(timespec="seconds")}
    with open(FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def all_bookings() -> list[dict]:
    try:
        with open(FILE, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def for_profile(slug: str, upcoming_only: bool = False) -> list[dict]:
    today = dt.date.today().isoformat()
    return [b for b in all_bookings() if b.get("profile") == slug and (not upcoming_only or b["day"] >= today)]
