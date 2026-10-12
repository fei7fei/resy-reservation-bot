"""Phone alerts via ntfy (https://ntfy.sh) -- free, no account needed.

Each profile picks a private topic name; anyone who subscribes to that topic in
the ntfy app gets the alerts, so topics are long random strings.

Profile config:
    alerts:
      enabled: true
      topic: resybot-3f9c2a71b0de
      booked: true       # a booking went through
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
    """Alerts for one profile's bot, with a little de-duplication so a busy day can't spam the phone.

    Alert settings are re-read from the profile file on every alert, so switching alerts on (or
    changing the topic) in the app takes effect for bots that are already running."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.slug = cfg.get("profile_slug")
        self.who = cfg.get("profile_name") or "Resy Bot"
        self.place = cfg.get("restaurant_name") or "the restaurant"
        self.url = cfg.get("restaurant_url") or "https://resy.com"
        self.dry = bool(cfg.get("dry_run", True))
        self._recent: dict[str, float] = {}

    def _settings(self) -> dict:
        if self.slug:
            from . import profiles  # local import: profiles is only needed when a bot actually alerts
            live = profiles.load(self.slug).get("alerts")
            if live is not None:
                return live
        return self.cfg.get("alerts") or {}

    def _topic(self, want: str) -> str | None:
        a = self._settings()
        return a.get("topic") if a.get("enabled") and a.get(want, True) else None

    def _once(self, key: str, every: float) -> bool:
        now = time.monotonic()
        if now - self._recent.get(key, -1e9) < every:
            return False
        self._recent[key] = now
        return True

    def booked(self, when: str, party: int):
        topic = self._topic("booked")
        if self.dry:
            send(topic, f"🧪 Dry run: would book {self.place}", f"{when} · party of {party} · {self.who}",
                 priority=3, tags=["test_tube"], click=self.url)
        else:
            send(topic, f"🎉 Booked {self.place}!",
                 f"{when} · party of {party} · on {self.who}'s Resy account. Check the Resy app for the confirmation.",
                 priority=5, tags=["tada"], click="https://resy.com/account")

    def missed_opening(self, when: str):
        if self._once(f"open:{when}", 1800):
            send(self._topic("openings"), f"Table opened at {self.place}",
                 f"{when} — someone else grabbed it first. Openings like this are worth watching for. ({self.who})",
                 priority=4, tags=["eyes"], click=self.url)

    def problem(self, title: str, message: str, key: str | None = None):
        if self._once(f"problem:{key or title}", 3600):
            send(self._topic("problems"), f"⚠️ {title}", f"{message} ({self.who})", priority=4, tags=["warning"])

    def info(self, title: str, message: str):
        send(self._topic("problems"), title, f"{message} ({self.who})", priority=3, tags=["information_source"])
