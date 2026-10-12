"""Resy auto-booker bot: two modes.

  watch  - catch cancellations on one or more dates. One calendar request covers
           every watched date, so it can check often without making many requests;
           a day's individual times are only fetched when that day shows a table.
  snipe  - wait for the moment new dates are released (e.g. 9:00 AM, 29 days out)
           and grab a table in your window the instant it appears.

Usage (the app starts these for you):
    python -m resybot.bot watch --config <snapshot.yaml> [-v]
    python -m resybot.bot snipe --config <snapshot.yaml> [-v]

Log lines starting with markers like "Opening spotted", "*** Booked" and
"TOKEN EXPIRED" are read by the app's dashboard -- keep them stable.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import random
import sys
import time

import requests
import yaml

from .client import (AuthError, NoAvailabilityError, RateLimited, ResyClient, ResyError, Slot,
                     slot_matches_window)
from . import bookings
from .notify import Notifier

log = logging.getLogger("resy")

EXIT_TOKEN = 3  # exit code when the login token is expired


def setup_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def keep_awake():
    """Stop Windows from sleeping while this process runs (the screen can still turn off).
    Released automatically when the process exits. Closing a laptop lid can still sleep it."""
    try:
        import ctypes

        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        log.info("Keeping the PC awake while the bot runs.")
    except Exception:
        pass


def beep():
    try:
        import winsound

        for _ in range(3):
            winsound.Beep(1000, 300)
            time.sleep(0.1)
    except Exception:
        pass


def nice_when(day: str, hhmm: str) -> str:
    d = dt.date.fromisoformat(day)
    t = dt.datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0")
    return f"{d:%a %b} {d.day} at {t}"


# ---------------------------------------------------------------------------
# slot choice + booking
# ---------------------------------------------------------------------------

def order_slots(matches: list[Slot], start_hhmm: str, preferred: str | None) -> list[Slot]:
    """Preferred time first (if open), then the rest earliest -> latest within the window."""
    def mins(hhmm: str) -> int:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    start = mins(start_hhmm)
    ordered = sorted(matches, key=lambda s: (mins(s.time_str) - start) % 1440)  # handles windows past midnight
    if preferred:
        ordered.sort(key=lambda s: s.time_str != preferred)  # stable: preferred to front
    return ordered


def matching_slots(slots: list[Slot], cfg: dict) -> list[Slot]:
    """Slots inside the time window (and seating types, if given), in the order to try them."""
    start, end = cfg["preferred_time_start"], cfg["preferred_time_end"]
    wanted = [w.strip().lower() for w in (cfg.get("seating_types") or []) if w.strip()]
    ok = [s for s in slots if slot_matches_window(s, start, end)
          and (not wanted or any(w in s.slot_type.lower() for w in wanted))]
    return order_slots(ok, start, cfg.get("preferred_time"))


def try_book_slot(client: ResyClient, slot: Slot, day: str, cfg: dict, alerts: Notifier) -> bool:
    """True on a confirmed booking (or a dry-run match). AuthError propagates."""
    party = cfg["party_size"]
    log.info("Attempting to book slot %s (%s)", slot.time_str, slot.slot_type)
    try:
        book_token, payment_methods = client.get_book_token(slot, day, party)
    except NoAvailabilityError:
        log.warning("Slot disappeared before we could get a book_token")
        return False
    except AuthError:
        raise
    except (ResyError, requests.RequestException) as e:  # transient 5xx etc. -- keep polling rather than crash
        log.warning("details failed, will retry: %s", e)
        return False

    pm_id = cfg.get("payment_method_id")
    if payment_methods and pm_id is None:
        pm_id = payment_methods[0].get("id")
        log.info("Venue requires a card on file; using payment_method_id=%s from your account", pm_id)

    if cfg.get("dry_run", False):
        log.info("DRY RUN: would book slot %s with pm=%s", slot.time_str, pm_id)
        log.info("*** DRY RUN match: %s %s ***", day, slot.time_str)
        alerts.booked(nice_when(day, slot.time_str), party)
        beep()
        return True

    try:
        result = client.book(book_token, pm_id)
    except NoAvailabilityError:
        log.warning("Booking rejected -- someone beat us to it")
        return False
    except AuthError:
        raise
    except (ResyError, requests.RequestException) as e:
        log.warning("book failed, will retry: %s", e)
        return False

    log.info("BOOKED: reservation %s", result.get("reservation_id"))
    log.info("*** Booked %s at %s! ***", day, slot.time_str)
    bookings.record(cfg, day, slot.time_str, result.get("reservation_id"))
    alerts.booked(nice_when(day, slot.time_str), party)
    beep()
    return True


def book_first(client: ResyClient, slots: list[Slot], day: str, cfg: dict, alerts: Notifier) -> bool:
    """Try matching slots in order; alert if a table showed up but every attempt lost the race."""
    matches = matching_slots(slots, cfg)
    if not matches:
        return False
    log.info("Opening spotted on %s: %s", day, [s.time_str for s in matches])
    for slot in matches:
        if try_book_slot(client, slot, day, cfg, alerts):
            return True
    alerts.missed_opening(nice_when(day, matches[0].time_str))
    return False


# ---------------------------------------------------------------------------
# watch mode
# ---------------------------------------------------------------------------

def run_watch(cfg: dict, client: ResyClient, alerts: Notifier):
    """Calendar-driven watch.

    Every `poll` seconds: one calendar request for all watched dates.
      * a date that just turned available -> fetch its times right away and try to book
      * a date that's available but had nothing in the window -> re-check its times every `find_every`
      * a sold-out date -> a slow safety-net check of its times every `fallback_every`,
        in case the calendar ever lags behind
    Backs off automatically if Resy ever answers 429.
    """
    venue, party = cfg["venue_id"], cfg["party_size"]
    days = sorted(cfg.get("watch_dates") or [cfg["reservation_date"]])
    poll = max(10, int(cfg.get("watch_poll_interval_seconds", 15)))
    find_every = max(30, poll * 2)
    fallback_every = 180
    stop_first = cfg.get("stop_after_first_booking", True)
    stop_at = cfg.get("watch_stop_at")
    stop_at = dt.datetime.strptime(stop_at, "%Y-%m-%d %H:%M:%S") if stop_at else None

    log.info("Watching venue %s on %s for openings in %s-%s, calendar every ~%ss",
             venue, days, cfg["preferred_time_start"], cfg["preferred_time_end"], poll)
    prev: dict[str, str] = {}
    next_find = {d: 0.0 for d in days}
    slow = 1  # multiplier after a 429
    while days:
        if stop_at and dt.datetime.now() >= stop_at:
            log.info("Reached watch_stop_at, giving up.")
            return
        days = [d for d in days if d >= dt.date.today().isoformat()]
        span = (dt.date.fromisoformat(max(days)) - dt.date.today()).days + 1 if days else 1
        try:
            cal = client.get_calendar(venue, party, days=span)
            statuses = {d["date"]: (d.get("inventory") or {}).get("reservation") for d in cal.get("scheduled", [])}
            slow = max(1, slow // 2)
        except RateLimited as e:
            slow = min(slow * 2, 16)
            log.warning("%s -- slowing down to every ~%ss", e, poll * slow)
            time.sleep(poll * slow)
            continue
        except (ResyError, requests.RequestException) as e:
            if isinstance(e, AuthError):
                raise
            log.warning("calendar check failed: %s", e)
            statuses = {}

        for day in list(days):
            status = statuses.get(day, "unknown")
            if status == "closed":
                continue
            now = time.monotonic()
            just_opened = status == "available" and prev.get(day) != "available"
            prev[day] = status
            if not (just_opened or now >= next_find[day]):
                continue
            wait = find_every if status in ("available", "unknown") else fallback_every
            next_find[day] = now + wait + random.uniform(0, wait * 0.2)
            try:
                slots = client.find_slots(venue, day, party)
            except RateLimited as e:
                slow = min(slow * 2, 16)
                log.warning("%s -- slowing down", e)
                break
            except (ResyError, requests.RequestException) as e:
                if isinstance(e, AuthError):
                    raise
                log.warning("find() failed for %s: %s", day, e)
                continue
            if book_first(client, slots, day, cfg, alerts):
                if stop_first:
                    return
                days.remove(day)  # booked this date; keep watching the others
            elif just_opened:
                log.info("%s opened, but nothing in your window (%s)", day,
                         ", ".join(sorted({s.time_str for s in slots})) or "no times listed")
            else:
                log.debug("No openings on %s", day)
        time.sleep(poll * slow + random.uniform(0, poll * 0.3))
    log.info("Booked every watched date -- done.")


# ---------------------------------------------------------------------------
# snipe mode
# ---------------------------------------------------------------------------

def snipe_targets(cfg: dict) -> list[tuple[dt.datetime, str]]:
    """(release moment, dining date) pairs in chronological order."""
    days = cfg.get("watch_dates") or [cfg["reservation_date"]]
    if cfg.get("snipe_days_ahead") is not None:
        n = int(cfg["snipe_days_ahead"])
        rel = dt.datetime.strptime(cfg.get("release_time", "09:00:00"), "%H:%M:%S").time()
        return sorted((dt.datetime.combine(dt.date.fromisoformat(d) - dt.timedelta(days=n), rel), d) for d in days)
    return [(dt.datetime.strptime(cfg["drop_at"], "%Y-%m-%d %H:%M:%S"), cfg["reservation_date"])]


def sleep_until(when: dt.datetime):
    while True:
        left = (when - dt.datetime.now()).total_seconds()
        if left <= 0:
            return
        time.sleep(min(left, 60) if left > 1 else 0.02)


def snipe_one(client: ResyClient, cfg: dict, day: str, drop_at: dt.datetime, alerts: Notifier) -> bool:
    """Sleep until drop_at, then check rapidly for a table on `day`. True on a booking."""
    lead = dt.timedelta(seconds=cfg.get("lead_seconds", 8))
    burst = cfg.get("burst_poll_interval_seconds", 0.4)
    timeout = cfg.get("burst_timeout_seconds", 75)

    if drop_at - lead > dt.datetime.now():
        log.info("Sleeping until %s (drop at %s for %s)", drop_at - lead, drop_at, day)
        # Re-check the login a few minutes before the drop, while there's still time to fix it.
        precheck = drop_at - dt.timedelta(minutes=5)
        if precheck > dt.datetime.now():
            sleep_until(precheck)
            client.check_token()  # AuthError -> alert + exit, five minutes before the drop
            log.info("Token still valid; ready for the %s drop.", drop_at.strftime("%I:%M %p"))
        sleep_until(drop_at - lead)
    else:
        log.warning("Drop for %s is now or just passed -- starting immediately", day)
    sleep_until(drop_at)

    log.info("Drop time reached. Checking for a slot in %s-%s on %s",
             cfg["preferred_time_start"], cfg["preferred_time_end"], day)
    deadline = time.monotonic() + timeout
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            slots = client.find_slots(cfg["venue_id"], day, cfg["party_size"])
        except AuthError:
            raise
        except RateLimited:
            log.warning("Rate limited during the drop -- easing off")
            time.sleep(2)
            continue
        except (ResyError, requests.RequestException) as e:
            log.debug("find() attempt %d failed: %s", attempt, e)
            time.sleep(burst)
            continue
        if book_first(client, slots, day, cfg, alerts):
            return True
        time.sleep(burst + random.uniform(0, 0.15))

    log.error("Timed out after %ss with no booking for %s", timeout, day)
    alerts.info("Snipe missed", f"No table in your window for {day} within {timeout}s of the release.")
    return False


def run_snipe(cfg: dict, client: ResyClient, alerts: Notifier):
    stop_first = cfg.get("stop_after_first_booking", True)
    timeout = cfg.get("burst_timeout_seconds", 75)
    targets = snipe_targets(cfg)
    log.info("Snipe plan: %s", ", ".join(f"{d} (opens {t:%a %b %d %H:%M:%S})" for t, d in targets))
    for drop_at, day in targets:
        if dt.datetime.now() > drop_at + dt.timedelta(seconds=timeout):
            log.warning("%s already opened at %s -- skipping (use Watch to catch cancellations)", day, drop_at)
            continue
        if snipe_one(client, cfg, day, drop_at, alerts) and stop_first:
            log.info("Booked one -- stopping (stop_after_first_booking is on).")
            return
    log.info("Snipe plan finished.")


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["snipe", "watch"])
    parser.add_argument("--config", required=True)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    keep_awake()
    alerts = Notifier(cfg)

    client = ResyClient(api_key=cfg.get("api_key"))
    if not cfg.get("auth_token"):
        log.error("No auth token set. Paste this profile's Resy token in the app.")
        sys.exit(2)
    client.set_auth_token(cfg["auth_token"])
    try:
        user = client.check_token()
        log.info("Signed in to Resy as %s", user.get("first_name") or "your account")
        if args.mode == "snipe":
            run_snipe(cfg, client, alerts)
        else:
            run_watch(cfg, client, alerts)
    except AuthError as e:
        log.error("TOKEN EXPIRED: %s", e)
        alerts.problem("Resy token expired", "The bot stopped. Open the app, paste a fresh token and start it again.",
                       key="token")
        sys.exit(EXIT_TOKEN)
    except KeyboardInterrupt:
        pass
    except Exception as e:  # anything unexpected: say so on the phone, then fail loudly in the log
        log.exception("Bot crashed: %s", e)
        alerts.problem("Bot stopped unexpectedly", f"{args.mode} bot error: {e}", key="crash")
        raise


if __name__ == "__main__":
    main()
