"""Resy auto-booker: two modes.

  snipe  - wait for an exact moment a booking window opens (e.g. "Tuesday
           10:00:00 AM, 30 days out") and grab a slot in your preferred
           time range the instant it appears.
  watch  - continuously poll a date for cancellations within a time range
           and grab the first opening.

Usage:
    python resy_bot.py snipe --config config.yaml
    python resy_bot.py watch --config config.yaml

See config.example.yaml for all settings and README.md for how to get
your api_key / venue_id / payment_method_id.
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

from resy_client import NoAvailabilityError, ResyClient, ResyError, Slot, slot_matches_window

log = logging.getLogger("resy")


def setup_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("resy_bot.log")],
    )


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def notify(message: str):
    log.info("*** %s ***", message)
    try:
        import winsound

        for _ in range(3):
            winsound.Beep(1000, 300)
            time.sleep(0.1)
    except Exception:
        pass


def keep_awake():
    """Stop Windows from sleeping while this process runs (the screen can still turn off).
    Released automatically when the bot exits. Closing a laptop lid can still sleep it."""
    try:
        import ctypes

        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        log.info("Keeping the PC awake while the bot runs.")
    except Exception:
        pass


def make_client(cfg: dict) -> ResyClient:
    client = ResyClient(api_key=cfg.get("api_key"))
    if cfg.get("auth_token"):
        client.set_auth_token(cfg["auth_token"])
    elif cfg.get("email") and cfg.get("password") and cfg["email"] != "you@example.com":
        client.login(cfg["email"], cfg["password"])
    else:
        log.error("No auth token set. Paste your X-Resy-Auth-Token (DevTools > Network > any api.resy.com "
                  "request > Request Headers) into the Auth token box and Save.")
        sys.exit(2)
    return client


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


def matching_slots(slots: list[Slot], start_hhmm: str, end_hhmm: str, preferred: str | None,
                   seating: list[str] | None) -> list[Slot]:
    """Slots inside the time window (and seating types, if given), in the order to try them."""
    wanted = [w.strip().lower() for w in (seating or []) if w.strip()]
    ok = [
        s for s in slots
        if slot_matches_window(s, start_hhmm, end_hhmm)
        and (not wanted or any(w in s.slot_type.lower() for w in wanted))
    ]
    return order_slots(ok, start_hhmm, preferred)


def try_book_slot(client: ResyClient, slot: Slot, day: str, party_size: int, payment_method_id: int | None, dry_run: bool) -> bool:
    """Returns True on confirmed booking."""
    log.info("Attempting to book slot %s (%s)", slot.time_str, slot.slot_type)
    try:
        book_token, payment_methods = client.get_book_token(slot, day, party_size)
    except NoAvailabilityError:
        log.warning("Slot disappeared before we could get a book_token")
        return False
    except (ResyError, requests.RequestException) as e:  # transient 5xx etc. -- keep polling rather than crash
        log.warning("details failed, will retry: %s", e)
        return False

    pm_id = payment_method_id
    if payment_methods and pm_id is None:
        pm_id = payment_methods[0].get("id")
        log.info("Venue requires a card on file; using payment_method_id=%s from your account", pm_id)

    if dry_run:
        log.info("DRY RUN: would book slot %s with book_token=%s pm=%s", slot.time_str, book_token, pm_id)
        notify(f"DRY RUN match: {day} {slot.time_str}")
        return True

    try:
        result = client.book(book_token, pm_id)
    except NoAvailabilityError:
        log.warning("Booking rejected -- someone beat us to it")
        return False
    except (ResyError, requests.RequestException) as e:
        log.warning("book failed, will retry: %s", e)
        return False

    log.info("BOOKED: %s", result)
    notify(f"Booked {day} at {slot.time_str}!")
    return True


# ---------------------------------------------------------------------------
# snipe mode
# ---------------------------------------------------------------------------

def snipe_targets(cfg: dict) -> list[tuple[dt.datetime, str]]:
    """(release moment, dining date) pairs in chronological order.

    With snipe_days_ahead + release_time, every date in watch_dates gets its own
    release moment (date - N days, at release_time). Otherwise falls back to the
    single drop_at for reservation_date."""
    days = cfg.get("watch_dates") or [cfg["reservation_date"]]
    if cfg.get("snipe_days_ahead") is not None:
        n = int(cfg["snipe_days_ahead"])
        rel = dt.datetime.strptime(cfg.get("release_time", "09:00:00"), "%H:%M:%S").time()
        return sorted((dt.datetime.combine(dt.date.fromisoformat(d) - dt.timedelta(days=n), rel), d) for d in days)
    return [(dt.datetime.strptime(cfg["drop_at"], "%Y-%m-%d %H:%M:%S"), cfg["reservation_date"])]


def snipe_one(client: ResyClient, cfg: dict, day: str, drop_at: dt.datetime) -> bool:
    """Sleep until drop_at, then hammer find() for `day`. True on a booking."""
    venue_id = cfg["venue_id"]
    party_size = cfg["party_size"]
    start_hhmm = cfg["preferred_time_start"]
    end_hhmm = cfg["preferred_time_end"]
    preferred = cfg.get("preferred_time")
    seating = cfg.get("seating_types")
    lead_seconds = cfg.get("lead_seconds", 8)
    burst_interval = cfg.get("burst_poll_interval_seconds", 0.3)
    timeout_seconds = cfg.get("burst_timeout_seconds", 90)
    payment_method_id = cfg.get("payment_method_id")
    dry_run = cfg.get("dry_run", False)

    wake_at = drop_at - dt.timedelta(seconds=lead_seconds)
    now = dt.datetime.now()
    if wake_at > now:
        sleep_for = (wake_at - now).total_seconds()
        log.info("Sleeping %.0fs until %s (drop at %s for %s)", sleep_for, wake_at, drop_at, day)
        time.sleep(sleep_for)
    else:
        log.warning("Drop for %s is now or just passed -- starting burst immediately", day)

    # Fine-grained spin for the last stretch so we don't oversleep past drop_at.
    while dt.datetime.now() < drop_at:
        time.sleep(0.02)

    log.info("Drop time reached. Hammering find() for a slot in %s-%s on %s", start_hhmm, end_hhmm, day)
    deadline = dt.datetime.now() + dt.timedelta(seconds=timeout_seconds)
    attempt = 0
    while dt.datetime.now() < deadline:
        attempt += 1
        try:
            slots = client.find_slots(venue_id, day, party_size)
        except (ResyError, requests.RequestException) as e:
            log.debug("find() attempt %d failed: %s", attempt, e)
            time.sleep(burst_interval)
            continue

        matches = matching_slots(slots, start_hhmm, end_hhmm, preferred, seating)
        if matches:
            log.info("Found %d matching slot(s) on attempt %d", len(matches), attempt)
            for slot in matches:
                if try_book_slot(client, slot, day, party_size, payment_method_id, dry_run):
                    return True
            # all matches got booked out from under us; keep polling until timeout
        time.sleep(burst_interval + random.uniform(0, 0.1))

    log.error("Timed out after %.0fs with no booking for %s", timeout_seconds, day)
    return False


def run_snipe(cfg: dict):
    client = make_client(cfg)
    stop_first = cfg.get("stop_after_first_booking", True)
    timeout_seconds = cfg.get("burst_timeout_seconds", 90)
    targets = snipe_targets(cfg)
    log.info("Snipe plan: %s", ", ".join(f"{d} (opens {t:%a %b %d %H:%M:%S})" for t, d in targets))

    for drop_at, day in targets:
        if dt.datetime.now() > drop_at + dt.timedelta(seconds=timeout_seconds):
            log.warning("%s already opened at %s -- skipping (use Watch to catch cancellations)", day, drop_at)
            continue
        if snipe_one(client, cfg, day, drop_at) and stop_first:
            log.info("Booked one -- stopping (stop_after_first_booking is on).")
            return
    log.info("Snipe plan finished.")


# ---------------------------------------------------------------------------
# watch mode (cancellation sniping)
# ---------------------------------------------------------------------------

def run_watch(cfg: dict):
    client = make_client(cfg)
    venue_id = cfg["venue_id"]
    party_size = cfg["party_size"]
    days = list(cfg.get("watch_dates") or [cfg["reservation_date"]])
    stop_first = cfg.get("stop_after_first_booking", True)
    start_hhmm = cfg["preferred_time_start"]
    end_hhmm = cfg["preferred_time_end"]
    preferred = cfg.get("preferred_time")
    seating = cfg.get("seating_types")
    poll_interval = cfg.get("watch_poll_interval_seconds", 20)
    payment_method_id = cfg.get("payment_method_id")
    dry_run = cfg.get("dry_run", False)
    stop_at = cfg.get("watch_stop_at")
    stop_at = dt.datetime.strptime(stop_at, "%Y-%m-%d %H:%M:%S") if stop_at else None

    log.info(
        "Watching venue %s on %s for openings in %s-%s, polling every %ss",
        venue_id, days, start_hhmm, end_hhmm, poll_interval,
    )

    consecutive_errors = 0
    while True:
        if stop_at and dt.datetime.now() >= stop_at:
            log.info("Reached watch_stop_at, giving up.")
            return

        for day in list(days):
            try:
                slots = client.find_slots(venue_id, day, party_size)
                consecutive_errors = 0
            except (ResyError, requests.RequestException) as e:
                consecutive_errors += 1
                log.warning("find() failed (%d in a row): %s", consecutive_errors, e)
                time.sleep(min(2 ** consecutive_errors, 120))  # quick retry for one-off 5xx, back off if it persists
                continue

            matches = matching_slots(slots, start_hhmm, end_hhmm, preferred, seating)
            if matches:
                log.info("Opening spotted on %s: %s", day, [s.time_str for s in matches])
                for slot in matches:
                    if try_book_slot(client, slot, day, party_size, payment_method_id, dry_run):
                        if stop_first:
                            return
                        days.remove(day)  # booked this date; keep watching the others
                        if not days:
                            log.info("Booked every watched date -- done.")
                            return
                        break
            else:
                log.debug("No openings on %s", day)

        time.sleep(poll_interval + random.uniform(0, poll_interval * 0.2))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["snipe", "watch"])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    cfg = load_config(args.config)
    keep_awake()

    if args.mode == "snipe":
        run_snipe(cfg)
    else:
        run_watch(cfg)


if __name__ == "__main__":
    main()
