"""Phone alerts via ntfy (https://ntfy.sh) -- free, no account needed.

Each profile picks a private topic name; anyone who subscribes to that topic in
the ntfy app gets the alerts, so topics are long random strings.

Profile config:
    alerts:
      enabled: true
      topic: resybot-3f9c2a71b0de
      openings: true     # a table appeared but the bot couldn't grab it
      problems: true     # token expired, bot crashed, snipe missed
"""
from __future__ import annotations

import logging
import secrets
import time

import requests

SERVER = "https://ntfy.sh"
log = logging.getLogger("resy")


def new_topic() -> str:
    return "resybot-" + secrets.token_hex(6)


def subscribe_url(topic: str) -> str:
    return f"{SERVER}/{topic}"


def send(topic: str, title: str, message: str, priority: int = 3, tags: list[str] | None = None,
         click: str | None = None) -> bool:
    """Push one alert. Never raises -- an alert failing must not stop a bot."""
    if not topic:
        return False
    body = {"topic": topic, "title": title, "message": message, "priority": priority, "tags": tags or []}
    if click:
        body["click"] = click
    try:
        r = requests.post(SERVER, json=body, timeout=10)
        return r.status_code == 200
    except requests.RequestException as e:
        log.debug("alert failed: %s", e)
        return False


class Notifier:
    """Alerts for one profile's bot, with a little de-duplication so a busy day can't spam the phone."""

    def __init__(self, cfg: dict):
        a = cfg.get("alerts") or {}
        self.topic = a.get("topic") if a.get("enabled") else None
        self.want_openings = a.get("openings", True)
        self.want_problems = a.get("problems", True)
        self.who = cfg.get("profile_name") or "Resy Bot"
        self.place = cfg.get("restaurant_name") or "the restaurant"
        self.url = cfg.get("restaurant_url") or "https://resy.com"
        self.dry = bool(cfg.get("dry_run", True))
        self._recent: dict[str, float] = {}

    def _once(self, key: str, every: float) -> bool:
        now = time.monotonic()
        if now - self._recent.get(key, -1e9) < every:
            return False
        self._recent[key] = now
        return True

    def booked(self, when: str, party: int):
        if self.dry:
            send(self.topic, f"🧪 Dry run: would book {self.place}", f"{when} · party of {party} · {self.who}",
                 priority=3, tags=["test_tube"], click=self.url)
        else:
            send(self.topic, f"🎉 Booked {self.place}!", f"{when} · party of {party} · {self.who}'s account",
                 priority=5, tags=["tada"], click="https://resy.com/account")

    def missed_opening(self, when: str):
        if self.want_openings and self._once(f"open:{when}", 1800):
            send(self.topic, f"Table opened at {self.place}",
                 f"{when} — someone else grabbed it first. Openings like this are worth watching for. ({self.who})",
                 priority=4, tags=["eyes"], click=self.url)

    def problem(self, title: str, message: str, key: str | None = None):
        if self.want_problems and self._once(f"problem:{key or title}", 3600):
            send(self.topic, f"⚠️ {title}", f"{message} ({self.who})", priority=4, tags=["warning"])

    def info(self, title: str, message: str):
        if self.want_problems:
            send(self.topic, title, f"{message} ({self.who})", priority=3, tags=["information_source"])
