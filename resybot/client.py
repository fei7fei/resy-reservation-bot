"""Thin wrapper around Resy's private web API.

Resy does not publish this API; it's the same one resy.com's own frontend
calls. Endpoints and field names can change without notice -- if booking
starts failing, open the Network tab on resy.com while manually booking
something and diff the requests against what's here.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import requests

BASE_URL = "https://api.resy.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# The public key resy.com's own frontend embeds in its JS (same for every user).
DEFAULT_API_KEY = "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"

log = logging.getLogger("resy")


class ResyError(Exception):
    """Raised for any non-recoverable API failure."""


class NoAvailabilityError(ResyError):
    """Raised when a find() call has no matching slot right now. Expected/common -- not a bug."""


class AuthError(ResyError):
    """The login token is expired or invalid (Resy answers 401/419). Needs a fresh token."""


class RateLimited(ResyError):
    """Resy answered 429: back off before trying again."""


def _check(resp: requests.Response, what: str, ok=(200,)):
    """Raise the right error for a non-OK response."""
    if resp.status_code in ok:
        return
    if resp.status_code in (401, 419):
        raise AuthError(f"{what}: Resy rejected the login token ({resp.status_code}) -- paste a fresh one")
    if resp.status_code == 429:
        raise RateLimited(f"{what}: rate limited (429)")
    raise ResyError(f"{what} failed ({resp.status_code}): {resp.text[:300]}")


@dataclass
class Slot:
    config_id: str
    slot_type: str
    start: str  # "YYYY-MM-DD HH:MM:SS"
    end: str
    raw: dict[str, Any]

    @property
    def time_str(self) -> str:
        return self.start.split(" ")[1][:5]  # "HH:MM"


class ResyClient:
    def __init__(self, api_key: str | None = None, auth_token: str | None = None):
        if not api_key or "PASTE" in api_key:
            api_key = DEFAULT_API_KEY
        self.api_key = api_key
        self.auth_token = auth_token
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f'ResyAPI api_key="{api_key}"',
                "User-Agent": USER_AGENT,
                "Origin": "https://resy.com",
                "Referer": "https://resy.com/",
                "Cache-Control": "no-cache",
            }
        )

    # -- auth -----------------------------------------------------------

    def login(self, email: str, password: str) -> str:
        resp = self.session.post(
            f"{BASE_URL}/3/auth/password",
            data={"email": email, "password": password},
            timeout=15,
        )
        if resp.status_code != 200:
            raise ResyError(f"login failed ({resp.status_code}): {resp.text[:300]}")
        token = resp.json().get("token")
        if not token:
            raise ResyError(f"login response missing token: {resp.text[:300]}")
        self.set_auth_token(token)
        log.info("Logged in to Resy as %s", email)
        return token

    def set_auth_token(self, token: str) -> None:
        self.auth_token = token
        self.session.headers.update(
            {
                "X-Resy-Auth-Token": token,
                "X-Resy-Universal-Auth": token,
            }
        )

    def _require_auth(self):
        if not self.auth_token:
            raise ResyError("not authenticated -- call login() or set_auth_token() first")

    def check_token(self) -> dict:
        """The signed-in user's profile. Raises AuthError if the token is expired."""
        self._require_auth()
        resp = self.session.get(f"{BASE_URL}/2/user", timeout=15)
        _check(resp, "token check")
        return resp.json()

    # -- venue lookup -----------------------------------------------------

    def search_venue(self, query: str, latitude: float = 40.7128, longitude: float = -74.0060) -> list[dict]:
        """Best-effort venue search. Field names here drift; if this stops
        working, get the venue_id manually: open the restaurant's resy.com
        page, open devtools Network tab, reload, and look at the `venue_id`
        query param on the `/4/find` request."""
        resp = self.session.post(
            f"{BASE_URL}/3/venuesearch/search",
            json={"geo": {"latitude": latitude, "longitude": longitude}, "query": query, "per_page": 10},
            timeout=15,
        )
        resp.raise_for_status()
        hits = resp.json().get("search", {}).get("hits", [])
        return hits

    def get_venue(self, url_slug: str, location: str) -> dict:
        """Venue details by the slug/city in its resy.com URL. No login needed."""
        resp = self.session.get(
            f"{BASE_URL}/3/venue", params={"url_slug": url_slug, "location": location}, timeout=15
        )
        if resp.status_code != 200:
            raise ResyError(f"venue lookup failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    def get_calendar(self, venue_id: int, party_size: int, days: int = 120) -> dict:
        # (used by the watch bot too: one request covers every date it's watching)
        """Per-day inventory ('available' / 'sold-out' / 'closed') plus
        last_calendar_day, the furthest date currently bookable. No login needed."""
        today = dt.date.today()
        resp = self.session.get(
            f"{BASE_URL}/4/venue/calendar",
            params={
                "venue_id": venue_id,
                "num_seats": party_size,
                "start_date": today.isoformat(),
                "end_date": (today + dt.timedelta(days=days)).isoformat(),
            },
            timeout=15,
        )
        _check(resp, "calendar")
        return resp.json()

    # -- availability -----------------------------------------------------

    def find_slots(self, venue_id: int, day: str, party_size: int) -> list[Slot]:
        """day: 'YYYY-MM-DD'. Returns [] if the venue has nothing open for that day
        (this is the normal/expected case while polling, not an error)."""
        self._require_auth()
        # Don't pass Content-Type explicitly: json= sets it, and an explicit one
        # changes header order, which makes /4/find return 500 every time.
        resp = self.session.post(
            f"{BASE_URL}/4/find",
            json={
                "lat": 0,
                "long": 0,
                "day": day,
                "party_size": party_size,
                "venue_id": venue_id,
            },
            timeout=10,
        )
        _check(resp, "find")
        data = resp.json()
        venues = data.get("results", {}).get("venues", [])
        if not venues:
            return []
        slots_raw = venues[0].get("slots", [])
        slots = []
        for s in slots_raw:
            cfg = s.get("config", {})
            date = s.get("date", {})
            slots.append(
                Slot(
                    config_id=cfg.get("token"),
                    slot_type=cfg.get("type", ""),
                    start=date.get("start", ""),
                    end=date.get("end", ""),
                    raw=s,
                )
            )
        return slots

    # -- booking -----------------------------------------------------

    def get_book_token(self, slot: Slot, day: str, party_size: int) -> tuple[str, list[dict]]:
        """Returns (book_token, payment_methods). payment_methods is [] if
        the venue doesn't require a card on file for this slot."""
        self._require_auth()
        resp = self.session.post(
            f"{BASE_URL}/3/details",
            json={"commit": 1, "config_id": slot.config_id, "day": day, "party_size": party_size},
            timeout=10,
        )
        if resp.status_code == 404:
            raise NoAvailabilityError("slot was taken before we could grab a book_token")
        _check(resp, "details", ok=(200, 201))
        data = resp.json()
        book_token = data.get("book_token", {}).get("value")
        if not book_token:
            raise ResyError(f"details response missing book_token: {resp.text[:300]}")
        payment_methods = data.get("user", {}).get("payment_methods", []) or []
        return book_token, payment_methods

    def book(self, book_token: str, payment_method_id: int | None = None) -> dict:
        self._require_auth()
        data = {"book_token": book_token}
        if payment_method_id is not None:
            data["struct_payment_method"] = json.dumps({"id": payment_method_id})
        resp = self.session.post(
            f"{BASE_URL}/3/book",
            data=data,
            timeout=15,
        )
        if resp.status_code == 400:
            raise NoAvailabilityError(f"booking rejected -- slot likely gone: {resp.text[:300]}")
        _check(resp, "book", ok=(200, 201))
        return resp.json()

    def cancel(self, resy_token: str) -> dict:
        """Cancel a reservation by the resy_token that book() returned."""
        self._require_auth()
        resp = self.session.post(f"{BASE_URL}/3/cancel", data={"resy_token": resy_token}, timeout=15)
        if resp.status_code not in (200, 201):
            raise ResyError(f"cancel failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()


def slot_matches_window(slot: Slot, start_hhmm: str, end_hhmm: str) -> bool:
    t = slot.time_str
    if start_hhmm <= end_hhmm:
        return start_hhmm <= t <= end_hhmm
    return t >= start_hhmm or t <= end_hhmm  # window crosses midnight


def parse_venue_url(url: str) -> tuple[str, str]:
    """'https://resy.com/cities/vancouver-bc/venues/annalena?date=...' -> ('vancouver-bc', 'annalena').
    Also handles the older '/cities/ny/carbone' form."""
    path = re.sub(r"^[a-z]+://[^/]+", "", url.strip()).split("?")[0].split("#")[0]
    parts = [p for p in path.split("/") if p]
    if "cities" not in parts:
        raise ValueError("Paste the restaurant's resy.com page link, e.g. https://resy.com/cities/ny/venues/carbone")
    i = parts.index("cities")
    rest = [p for p in parts[i + 1:] if p != "venues"]
    if len(rest) < 2:
        raise ValueError("That link doesn't include a restaurant -- open the restaurant's own page and copy its link.")
    return rest[0], rest[1]


def booking_policy(venue: dict) -> dict:
    """Best-effort read of a venue's 'Need to know' text: how many days ahead
    bookings open, and at what time if stated."""
    text = " ".join((c.get("body") or "") for c in venue.get("content", []) or [] if c.get("name") == "need_to_know")
    out: dict = {"text": text.strip()}
    m = re.search(r"(\d+)\s*days?\s*(?:in advance|ahead|out|prior)", text, re.I)
    if m:
        out["days_ahead"] = int(m.group(1))
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)", text, re.I)
    if m:
        h = int(m.group(1)) % 12 + (12 if m.group(3).lower().startswith("p") else 0)
        out["release_time"] = f"{h:02d}:{int(m.group(2) or 0):02d}:00"
    return out
