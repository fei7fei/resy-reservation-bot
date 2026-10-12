"""Streamlit UI for resy_bot.

Run:  streamlit run resy_app.py      (or double-click the "Resy Bot" desktop shortcut)

Profiles: each profile is one Resy account with its own settings and its own
Watch/Snipe bots. The default profile uses config.yaml (+ .runs/); others live
in profiles/<name>.yaml (+ profiles/<name>.runs/).

Bots run as separate background processes, so they keep going if you close
the browser tab. Their output goes to <run dir>/<mode>.log, which this page tails.
"""
from __future__ import annotations

import datetime as dt
import glob
import html
import json
import os
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace

import altair as alt
import pandas as pd
import psutil
import streamlit as st
import yaml

from resy_client import ResyClient, booking_policy, parse_venue_url

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN_CONFIG = os.environ.get("RESY_CONFIG", os.path.join(HERE, "config.yaml"))
PROFILES_DIR = os.path.join(os.path.dirname(MAIN_CONFIG), "profiles")
MAIN = "main"
BOT = os.path.join(HERE, "resy_bot.py")
MODES = ("watch", "snipe")
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
# Settings that belong to an account, not copied into a new profile.
PERSONAL_KEYS = ("auth_token", "api_key", "email", "password", "payment_method_id", "profile_name", "ui_theme")

TIME_VALUES = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 15, 30, 45)]


def to_12h(hhmm: str) -> str:
    return dt.datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0")


def from_12h(label: str) -> str:
    return dt.datetime.strptime(label, "%I:%M %p").strftime("%H:%M")


TIME_LABELS = [to_12h(t) for t in TIME_VALUES]


def nice_date(d: dt.date) -> str:
    return f"{d:%a, %b} {d.day}"


# ---------------------------------------------------------------------------
# profiles
# ---------------------------------------------------------------------------

def current() -> str:
    return st.session_state.get("profile", MAIN)


def cfg_path(p: str | None = None) -> str:
    p = p or current()
    return MAIN_CONFIG if p == MAIN else os.path.join(PROFILES_DIR, f"{p}.yaml")


def run_dir(p: str | None = None) -> str:
    p = p or current()
    return os.path.join(os.path.dirname(MAIN_CONFIG), ".runs") if p == MAIN else os.path.join(PROFILES_DIR, f"{p}.runs")


def list_profiles() -> list[str]:
    others = sorted(os.path.splitext(os.path.basename(f))[0] for f in glob.glob(os.path.join(PROFILES_DIR, "*.yaml")))
    return [MAIN] + others


def profile_label(p: str) -> str:
    return load_cfg(p).get("profile_name") or ("Me" if p == MAIN else p)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]


def reset_form():
    """Forget the loaded profile's form values so init_state() reloads from the new profile."""
    ss = st.session_state
    for k in list(defaults({})) + ["cfg", "form", "lookup_error"]:
        ss.pop(k, None)


def switch_profile():
    st.session_state.profile = st.session_state.profile_pick
    st.query_params["profile"] = st.session_state.profile
    reset_form()


def create_profile():
    ss = st.session_state
    name = ss.new_profile_name.strip()
    slug = slugify(name)
    if not slug or slug == MAIN or os.path.exists(cfg_path(slug)):
        ss.profile_msg = "Pick a different name." if name else "Type a name first."
        return
    os.makedirs(PROFILES_DIR, exist_ok=True)
    # Start from the current profile's restaurant/dates/times, but never its account details.
    base = {k: v for k, v in load_cfg().items() if k not in PERSONAL_KEYS}
    base.update(profile_name=name, dry_run=True)
    save_cfg(base, cfg_path(slug))
    ss.new_profile_name = ""
    ss.profile_msg = None
    ss.profile_pick = slug
    switch_profile()


def delete_profile():
    p = current()
    if p == MAIN or any(bot_status(m, p) for m in MODES):
        return
    os.remove(cfg_path(p))
    shutil.rmtree(run_dir(p), ignore_errors=True)
    st.session_state.profile_pick = MAIN
    switch_profile()


# ---------------------------------------------------------------------------
# config + bot processes
# ---------------------------------------------------------------------------

def load_cfg(p: str | None = None) -> dict:
    try:
        with open(cfg_path(p), "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def save_cfg(c: dict, path: str | None = None):
    with open(path or cfg_path(), "w", encoding="utf-8") as f:
        f.write("# Written by resy_app.py. Contains credentials -- don't share or commit.\n")
        yaml.safe_dump(c, f, sort_keys=False, allow_unicode=True)


def python_exe() -> str:
    """python.exe, not pythonw.exe -- the bot needs a real stdout to write its log."""
    exe = sys.executable
    alt = os.path.join(os.path.dirname(exe), "python.exe")
    return alt if os.path.basename(exe).lower() == "pythonw.exe" and os.path.exists(alt) else exe


def bot_status(mode: str, p: str | None = None) -> dict | None:
    """Run info if this profile's bot for `mode` is alive, else None."""
    try:
        with open(os.path.join(run_dir(p), f"{mode}.json"), "r", encoding="utf-8") as f:
            info = json.load(f)
        proc = psutil.Process(info["pid"])
        cmd = " ".join(proc.cmdline())
        if proc.is_running() and "resy_bot.py" in cmd and f" {mode} " in f" {cmd} ":
            return info
    except (OSError, ValueError, KeyError, psutil.Error):
        pass
    return None


def start_bot(mode: str, cfg: dict, p: str | None = None):
    rd = run_dir(p)
    os.makedirs(rd, exist_ok=True)
    run_cfg = os.path.join(rd, f"{mode}.yaml")  # snapshot: later edits don't affect a running bot
    save_cfg(cfg, run_cfg)
    log = open(os.path.join(rd, f"{mode}.log"), "w", encoding="utf-8")
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    proc = subprocess.Popen(
        [python_exe(), "-u", BOT, mode, "--config", run_cfg, "-v"],
        cwd=HERE, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags,
    )
    log.close()
    with open(os.path.join(rd, f"{mode}.json"), "w", encoding="utf-8") as f:
        json.dump({"pid": proc.pid, "started": dt.datetime.now().isoformat(timespec="seconds"),
                   "dates": cfg["watch_dates"], "live": not cfg.get("dry_run", True),
                   "restaurant": cfg.get("restaurant_name", "")}, f)


def stop_bot(mode: str, p: str | None = None):
    info = bot_status(mode, p)
    if info:
        try:
            psutil.Process(info["pid"]).terminate()
        except psutil.Error:
            pass


def tail(mode: str, n: int = 300) -> str:
    try:
        with open(os.path.join(run_dir(), f"{mode}.log"), "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except FileNotFoundError:
        return ""


def run_summary(run: dict) -> str:
    kind = ":red[**LIVE**]" if run["live"] else ":blue[dry run]"
    since = dt.datetime.fromisoformat(run["started"])
    dates = ", ".join(nice_date(dt.date.fromisoformat(d)) for d in run["dates"])
    where = f"{run['restaurant']} · " if run.get("restaurant") else ""
    return f"{kind} · :gray[{where}{dates} · since {since:%a %I:%M %p}]"


# ---------------------------------------------------------------------------
# restaurant lookup
# ---------------------------------------------------------------------------

def lookup_restaurant():
    """on_click for Look up: fetch venue + calendar, store results in config and widget state."""
    ss = st.session_state
    ss.lookup_error = None
    try:
        location, slug = parse_venue_url(ss.url)
        client = ResyClient(api_key=ss.api_key or None)
        venue = client.get_venue(slug, location)
        venue_id = venue["id"]["resy"]
        cal = client.get_calendar(venue_id, int(ss.party))
    except Exception as e:  # bad link / network / API change -> show on page
        ss.lookup_error = str(e)
        return

    policy = booking_policy(venue)
    release = policy.get("release_time") or ss.release.strftime("%H:%M:%S")
    last_day = cal.get("last_calendar_day")
    # The calendar is ground truth (venue text can be off by one); fall back to the text.
    days_ahead = None
    if last_day:
        days_ahead = (dt.date.fromisoformat(last_day) - dt.date.today()).days
        if dt.datetime.now().time() < dt.datetime.strptime(release, "%H:%M:%S").time():
            days_ahead += 1  # before today's release, today's new date hasn't unlocked yet
    days_ahead = days_ahead or policy.get("days_ahead")

    statuses: dict[int, set] = {}
    for d in cal.get("scheduled", []):
        statuses.setdefault(dt.date.fromisoformat(d["date"]).weekday(), set()).add((d.get("inventory") or {}).get("reservation"))
    closed = sorted(wd for wd, s in statuses.items() if s == {"closed"})
    open_days = [d["date"] for d in cal.get("scheduled", []) if (d.get("inventory") or {}).get("reservation") == "available"]

    cfg = load_cfg()
    cfg.update(
        venue_id=venue_id,
        restaurant_name=venue.get("name", "?"),
        restaurant_url=ss.url.strip(),
        closed_weekdays=closed,
        venue_info={
            "city": (venue.get("location") or {}).get("locality", ""),
            "min_party": venue.get("min_party_size") or 1,
            "max_party": venue.get("max_party_size") or 20,
            "days_ahead": days_ahead,
            "release_listed": bool(policy.get("release_time")),
            "last_day": last_day,
            "open_days": open_days,
            "need_to_know": policy.get("text", ""),
            "checked": dt.datetime.now().strftime("%b %d, %I:%M %p"),
        },
    )
    if days_ahead:
        cfg["snipe_days_ahead"] = days_ahead
        ss.days_ahead = days_ahead
    cfg["release_time"] = release
    ss.release = dt.datetime.strptime(release, "%H:%M:%S").time()
    save_cfg(cfg)
    ss.cfg = cfg
    lo, hi = cfg["venue_info"]["min_party"], cfg["venue_info"]["max_party"]
    ss.party = min(max(int(ss.party), lo), hi)
    ss.dates = [d for d in ss.dates if d.weekday() not in closed]


# ---------------------------------------------------------------------------
# form state <-> config
# ---------------------------------------------------------------------------

def defaults(c: dict) -> dict:
    """Initial form values from the profile's config."""
    today = dt.date.today()
    dates = c.get("watch_dates") or ([c["reservation_date"]] if c.get("reservation_date") else [])
    stop_at = c.get("watch_stop_at")
    stop_dt = dt.datetime.strptime(stop_at, "%Y-%m-%d %H:%M:%S") if stop_at else dt.datetime.now() + dt.timedelta(days=1)
    return {
        "profile_name": c.get("profile_name") or ("Me" if current() == MAIN else current()),
        "url": c.get("restaurant_url", ""),
        "party": int(c.get("party_size", 2)),
        "dates": sorted({dt.date.fromisoformat(str(d)) for d in dates if dt.date.fromisoformat(str(d)) >= today}),
        "window": (to_12h(c.get("preferred_time_start", "18:00")), to_12h(c.get("preferred_time_end", "21:00"))),
        "pref": to_12h(c["preferred_time"]) if c.get("preferred_time") else "No preference",
        "seating": ", ".join(c.get("seating_types") or []),
        "poll": int(c.get("watch_poll_interval_seconds", 30)),
        "use_stop_at": bool(stop_at),
        "stop_date": stop_dt.date(),
        "stop_time": stop_dt.time().replace(second=0, microsecond=0),
        "days_ahead": int(c.get("snipe_days_ahead", 30)),
        "release": dt.datetime.strptime(c.get("release_time", "09:00:00"), "%H:%M:%S").time(),
        "token": c.get("auth_token", "") or "",
        "api_key": c.get("api_key", "") or "",
        "dry_run": bool(c.get("dry_run", True)),
        "stop_first": bool(c.get("stop_after_first_booking", True)),
    }


def init_state():
    ss = st.session_state
    if "profile" not in ss:
        wanted = st.query_params.get("profile", MAIN)
        ss.profile = wanted if wanted in list_profiles() else MAIN
    ss.profile_pick = ss.profile
    ss.setdefault("profile_msg", None)
    ss.setdefault("new_profile_name", "")
    first_run = "cfg" not in ss
    if first_run:
        ss.cfg = load_cfg()
        ss.lookup_error = None
    for k, v in defaults(ss.cfg).items():
        if k not in ss:
            ss[k] = v
        else:
            # Re-assign so Streamlit doesn't drop values of widgets that are hidden on this run
            # (e.g. the stop-time inputs while "Stop at a set time" is unchecked).
            ss[k] = ss[k]
    if first_run and ss.url and not ss.cfg.get("venue_info"):
        lookup_restaurant()  # config from before lookups were saved: fill in the restaurant card
    w = ss.window
    if not (isinstance(w, (tuple, list)) and len(w) == 2 and all(x in TIME_LABELS for x in w)):
        ss.window = defaults(ss.cfg)["window"]


def form_values():
    """The form as plain values. Inside the auto-refreshing bot panel (a fragment rerun),
    widgets outside the panel aren't re-rendered and their session_state entries come back
    in Streamlit's internal format -- so the panel reads this snapshot instead."""
    return st.session_state.get("form") or st.session_state


def snapshot_form():
    """Called at the end of every full run, after all form widgets have rendered."""
    ss = st.session_state
    st.session_state.form = SimpleNamespace(**{k: ss[k] for k in defaults({})})


def snipe_plan(values=None) -> list[tuple[dt.datetime, dt.date]]:
    ss = values or form_values()
    return sorted((dt.datetime.combine(d - dt.timedelta(days=int(ss.days_ahead)), ss.release), d) for d in ss.dates)


def collect_cfg() -> tuple[dict | None, str | None]:
    """Form -> config dict, or (None, error message)."""
    ss = form_values()
    c = dict(load_cfg())
    c["profile_name"] = ss.profile_name.strip() or c.get("profile_name") or "Me"
    if not c.get("venue_id"):
        return None, "Look up a restaurant first."
    if not ss.dates:
        return None, "Pick at least one date."
    if not ss.token.strip():
        return None, "Paste this profile's Resy auth token in the sidebar."
    start, end = from_12h(ss.window[0]), from_12h(ss.window[1])
    dates = [d.isoformat() for d in sorted(ss.dates)]
    plan = snipe_plan()
    c.update(
        party_size=int(ss.party),
        reservation_date=dates[0],
        watch_dates=dates,
        preferred_time_start=start,
        preferred_time_end=end,
        watch_poll_interval_seconds=max(10, int(ss.poll)),
        snipe_days_ahead=int(ss.days_ahead),
        release_time=ss.release.strftime("%H:%M:%S"),
        drop_at=plan[0][0].strftime("%Y-%m-%d %H:%M:%S"),
        auth_token=ss.token.strip(),
        dry_run=bool(ss.dry_run),
        stop_after_first_booking=bool(ss.stop_first),
    )
    if ss.pref != "No preference":
        c["preferred_time"] = from_12h(ss.pref)
    else:
        c.pop("preferred_time", None)
    seating = [x.strip() for x in ss.seating.split(",") if x.strip()]
    if seating:
        c["seating_types"] = seating
    else:
        c.pop("seating_types", None)
    if ss.use_stop_at:
        c["watch_stop_at"] = dt.datetime.combine(ss.stop_date, ss.stop_time).strftime("%Y-%m-%d %H:%M:%S")
    else:
        c.pop("watch_stop_at", None)
    if ss.api_key.strip():
        c["api_key"] = ss.api_key.strip()
    else:
        c.pop("api_key", None)  # client falls back to resy.com's public key
    return c, None


def save_settings() -> dict | None:
    c, err = collect_cfg()
    if err:
        st.toast(err, icon="⚠️")
        return None
    save_cfg(c)
    st.session_state.cfg = c
    return c


@st.dialog("Start a LIVE bot?")
def confirm_live(mode: str, cfg: dict, p: str | None = None):
    who = cfg.get("profile_name") or "this profile"
    st.write(f"Dry run is **off**. The {mode} bot will make a **real booking** on **{who}**'s Resy account for:")
    st.write(", ".join(nice_date(dt.date.fromisoformat(d)) for d in cfg["watch_dates"]))
    c1, c2 = st.columns(2)
    if c1.button("Yes, start it", type="primary", width="stretch"):
        start_bot(mode, cfg, p)
        st.rerun()
    if c2.button("Cancel", width="stretch"):
        st.rerun()


def request_start(mode: str) -> bool:
    """True if a bot was started right away (dry run); live runs go through a confirm dialog."""
    cfg = save_settings()
    if not cfg:
        return False
    if mode == "snipe":
        grace = dt.timedelta(seconds=cfg.get("burst_timeout_seconds", 90))
        if all(when < dt.datetime.now() - grace for when, _ in snipe_plan()):
            st.toast("Every selected date has already opened — use Watch to catch cancellations.", icon="⚠️")
            return False
    if cfg.get("dry_run"):
        start_bot(mode, cfg)
        return True
    confirm_live(mode, cfg)  # starts the bot if the user confirms
    return False


# ---------------------------------------------------------------------------
# dashboard data (parsed from each bot's log)
# ---------------------------------------------------------------------------

# Categorical slots in fixed order (validated reference palette), one per profile.
SERIES = {"light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"],
          "dark": ["#3987e5", "#d95926", "#199e70", "#c98500"]}
LINE_RE = re.compile(r"^(\d\d):(\d\d):(\d\d) \[(\w+)\] (.*)$")
BOOKED_RE = re.compile(r"\*\*\* (?:Booked (\d{4}-\d\d-\d\d) at (\d\d:\d\d)!|DRY RUN match: (\d{4}-\d\d-\d\d) (\d\d:\d\d)) \*\*\*")


def theme_mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:
        return "light"


def profile_color(p: str) -> str:
    profiles = list_profiles()
    return SERIES[theme_mode()][profiles.index(p) % 4 if p in profiles else 0]


def run_info(mode: str, p: str) -> dict | None:
    """Last run's metadata (running or not)."""
    try:
        with open(os.path.join(run_dir(p), f"{mode}.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def parse_log(mode: str, p: str) -> dict:
    """Checks, openings, bookings and errors from a bot's log (timestamps are HH:MM:SS)."""
    out = {"checks": [], "openings": 0, "booked": [], "errors": 0}
    meta = run_info(mode, p)
    path = os.path.join(run_dir(p), f"{mode}.log")
    if not meta or not os.path.exists(path):
        return out
    day = dt.datetime.fromisoformat(meta["started"]).date()
    prev = None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = LINE_RE.match(line)
            if not m:
                continue
            t = dt.time(int(m[1]), int(m[2]), int(m[3]))
            if prev and t < prev:
                day += dt.timedelta(days=1)  # log rolled past midnight
            prev = t
            when = dt.datetime.combine(day, t)
            msg = m[5]
            if "POST /4/find" in msg:
                out["checks"].append(when)
            elif msg.startswith("Opening spotted") or msg.startswith("Found "):
                out["openings"] += 1
            elif m[4] in ("WARNING", "ERROR"):
                out["errors"] += 1
            b = BOOKED_RE.search(msg)
            if b:
                out["booked"].append({"day": b[1] or b[3], "time": b[2] or b[4], "dry": bool(b[3])})
    return out


def ago(when: dt.datetime | None) -> str:
    if not when:
        return "—"
    s = int((dt.datetime.now() - when).total_seconds())
    return f"{s}s ago" if s < 90 else f"{s // 60}m ago" if s < 5400 else f"{s // 3600}h ago"


def countdown(when: dt.datetime) -> str:
    s = max(0, int((when - dt.datetime.now()).total_seconds()))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return (f"{d}d " if d else "") + f"{h:02d}:{m:02d}:{s:02d}"


def next_release(c: dict) -> dt.datetime | None:
    if c.get("snipe_days_ahead") is None or not c.get("watch_dates"):
        return None
    rel = dt.datetime.strptime(c.get("release_time", "09:00:00"), "%H:%M:%S").time()
    upcoming = [dt.datetime.combine(dt.date.fromisoformat(d) - dt.timedelta(days=int(c["snipe_days_ahead"])), rel)
                for d in c["watch_dates"]]
    upcoming = [u for u in upcoming if u > dt.datetime.now()]
    return min(upcoming) if upcoming else None


def start_saved(mode: str, p: str):
    """Start a bot from a profile's saved settings (dashboard Start button)."""
    c = load_cfg(p)
    missing = [label for key, label in (("venue_id", "a restaurant"), ("watch_dates", "dates"), ("auth_token", "a token"))
               if not c.get(key)]
    if missing:
        st.toast(f"{profile_label(p)} needs {', '.join(missing)} — open Edit.", icon="⚠️")
        return
    if mode == "snipe" and not next_release(c):
        st.toast(f"All of {profile_label(p)}'s dates have already opened — use Watch.", icon="⚠️")
        return
    if c.get("dry_run", True):
        start_bot(mode, c, p)
    else:
        confirm_live(mode, c, p)


def edit_profile(p: str):
    """Open profile p in the Settings view. Called mid-run, so it can't touch widget keys
    that are already drawn (the sidebar picker); init_state() syncs them on the rerun."""
    st.session_state.profile = p
    st.query_params["profile"] = p
    reset_form()
    st.session_state["_goto_settings"] = True


CSS = """
<style>
.rb-card {border-radius: 18px; padding: 18px 20px 14px; margin-bottom: 6px;
  background: linear-gradient(135deg, color-mix(in srgb, var(--rb-c) 16%, transparent), rgba(127,127,127,.04) 65%);
  border: 1px solid color-mix(in srgb, var(--rb-c) 45%, rgba(127,127,127,.25));}
.rb-head {display:flex; align-items:center; gap:12px;}
.rb-avatar {width:44px; height:44px; border-radius:50%; background:var(--rb-c); color:#fff;
  display:flex; align-items:center; justify-content:center; font-weight:700; font-size:20px; flex:none;}
.rb-name {font-size:20px; font-weight:700; line-height:1.15;}
.rb-sub {opacity:.72; font-size:13px;}
.rb-pills {display:flex; flex-wrap:wrap; gap:6px; margin:12px 0 8px;}
.rb-pill {display:inline-flex; align-items:center; gap:6px; padding:3px 10px; border-radius:999px;
  font-size:12.5px; font-weight:600; border:1px solid rgba(127,127,127,.35);}
.rb-pill.on {border-color: rgba(12,163,12,.6); background: rgba(12,163,12,.12);}
.rb-pill.live {border-color: rgba(208,59,59,.65); background: rgba(208,59,59,.14);}
.rb-dot {width:8px; height:8px; border-radius:50%; background:#0ca30c; animation: rb-pulse 1.6s infinite;}
.rb-dot.off {background: rgba(127,127,127,.6); animation:none;}
@keyframes rb-pulse {0%{box-shadow:0 0 0 0 rgba(12,163,12,.55)} 70%{box-shadow:0 0 0 8px rgba(12,163,12,0)} 100%{box-shadow:0 0 0 0 rgba(12,163,12,0)}}
.rb-chips {display:flex; flex-wrap:wrap; gap:6px; margin:6px 0 10px;}
.rb-chip {padding:3px 9px; border-radius:8px; font-size:12.5px; background: rgba(127,127,127,.13);}
.rb-chip.booked {background: rgba(12,163,12,.2); font-weight:700;}
.rb-stats {display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-top:4px;}
.rb-stat b {display:block; font-size:22px; line-height:1.2;}
.rb-stat span {opacity:.68; font-size:12px;}
.rb-countdown {font-family: ui-monospace, Consolas, monospace; font-weight:700;}
.rb-booked {margin-top:10px; padding:8px 12px; border-radius:10px; background: rgba(12,163,12,.16);
  border:1px solid rgba(12,163,12,.5); font-weight:700;}
</style>
"""


def profile_card_html(p: str, c: dict, runs: dict, stats: dict) -> str:
    name = html.escape(profile_label(p))
    rest = html.escape(c.get("restaurant_name") or "No restaurant yet")
    win = ""
    if c.get("preferred_time_start"):
        win = f" · {to_12h(c['preferred_time_start'])}–{to_12h(c['preferred_time_end'])}"
    pills = []
    for mode, icon, verb in (("watch", "👀", "Watching"), ("snipe", "🎯", "Snipe armed")):
        if runs.get(mode):
            pills.append(f'<span class="rb-pill on"><span class="rb-dot"></span>{icon} {verb}</span>')
    if not pills:
        pills.append('<span class="rb-pill"><span class="rb-dot off"></span>Idle</span>')
    if any(runs.values()):
        live = any(r["live"] for r in runs.values() if r)
        pills.append('<span class="rb-pill live">● LIVE</span>' if live else '<span class="rb-pill">🧪 Dry run</span>')
    nxt = next_release(c)
    if runs.get("snipe") and nxt:
        pills.append(f'<span class="rb-pill">⏱ next drop in&nbsp;<span class="rb-countdown">{countdown(nxt)}</span></span>')

    real = [b for b in stats["booked"] if not b["dry"]]
    booked_days = {b["day"] for b in real}
    chips = "".join(
        f'<span class="rb-chip{" booked" if d in booked_days else ""}">'
        f'{"✓ " if d in booked_days else ""}{nice_date(dt.date.fromisoformat(d))}</span>'
        for d in (c.get("watch_dates") or []))
    banner = ""
    if real:
        b = real[-1]
        banner = f'<div class="rb-booked">🎉 Booked {nice_date(dt.date.fromisoformat(b["day"]))} at {to_12h(b["time"])}</div>'
    elif stats["booked"]:
        b = stats["booked"][-1]
        banner = (f'<div class="rb-chip" style="margin-top:8px">🧪 Dry-run match: '
                  f'{nice_date(dt.date.fromisoformat(b["day"]))} at {to_12h(b["time"])}</div>')
    last = stats["checks"][-1] if stats["checks"] else None
    return f"""
<div class="rb-card" style="--rb-c:{profile_color(p)}">
  <div class="rb-head"><div class="rb-avatar">{name[:1].upper()}</div>
    <div><div class="rb-name">{name}</div><div class="rb-sub">{rest} · party of {c.get("party_size", 2)}{win}</div></div></div>
  <div class="rb-pills">{''.join(pills)}</div>
  <div class="rb-chips">{chips or '<span class="rb-chip">No dates picked</span>'}</div>
  <div class="rb-stats">
    <div class="rb-stat"><b>{len(stats['checks']):,}</b><span>checks</span></div>
    <div class="rb-stat"><b>{ago(last)}</b><span>last check</span></div>
    <div class="rb-stat"><b>{stats['openings']}</b><span>openings seen</span></div>
  </div>
  {banner}
</div>"""


def activity_chart(series: dict[str, list[dt.datetime]], colors: list[str]):
    """Checks per 10 minutes over the last 6 hours, one line per profile."""
    now = dt.datetime.now()
    start = (now - dt.timedelta(hours=6)).replace(second=0, microsecond=0)
    start -= dt.timedelta(minutes=start.minute % 10)
    buckets = pd.date_range(start, now, freq="10min")
    rows = []
    for name, times in series.items():
        recent = [t for t in times if t >= start]
        counts = (pd.Series(1, index=pd.DatetimeIndex(recent)).resample("10min", origin=start).sum()
                  .reindex(buckets, fill_value=0)) if recent else pd.Series(0, index=buckets)
        rows += [{"time": t, "profile": name, "checks": int(v)} for t, v in counts.items()]
    df = pd.DataFrame(rows)
    names = list(series)
    legend = alt.Legend(orient="top", title=None) if len(names) > 1 else None
    color = alt.Color("profile:N", scale=alt.Scale(domain=names, range=colors), legend=legend)
    hover = alt.selection_point(fields=["time"], nearest=True, on="pointerover", empty=False)
    base = alt.Chart(df).encode(
        x=alt.X("time:T", title=None, axis=alt.Axis(format="%-I:%M %p", grid=False)),
        y=alt.Y("checks:Q", title="checks / 10 min", axis=alt.Axis(gridOpacity=0.15, tickMinStep=1)),
        color=color)
    lines = base.mark_line(strokeWidth=2, interpolate="monotone")
    points = base.mark_point(size=80, filled=True).encode(
        opacity=alt.condition(hover, alt.value(1), alt.value(0)),
        tooltip=[alt.Tooltip("profile:N", title="Profile"), alt.Tooltip("time:T", title="Time", format="%-I:%M %p"),
                 alt.Tooltip("checks:Q", title="Checks")]).add_params(hover)
    rule = alt.Chart(df).mark_rule(opacity=0.3).encode(x="time:T").transform_filter(hover)
    st.altair_chart((lines + points + rule).properties(height=230), width="stretch")


STATUS_FILL = {"light": {"open now": "#2a78d6", "sold out": "#d03b3b", "closed": "#e6e6e3"},
               "dark": {"open now": "#3987e5", "sold out": "#c03535", "closed": "#2a2a28"}}


@st.cache_data(ttl=60, show_spinner=False)
def live_calendar(venue_id: int, party: int) -> dict:
    """Resy's availability calendar for a venue (no login needed); cached for a minute."""
    return ResyClient().get_calendar(venue_id, party, days=60)


def availability_calendar(venue_id: int, party: int, watchers: list[tuple[str, list[str], set[str]]], mode: str):
    """Every bookable day: open now / sold out / closed, with each profile's watched (●) and booked (✓) dates."""
    try:
        cal = live_calendar(venue_id, party)
    except Exception as e:  # network / API hiccup: keep the rest of the dashboard working
        st.caption(f"Calendar unavailable right now ({e}).")
        return
    today = dt.date.today().isoformat()
    horizon = (dt.date.today() + dt.timedelta(days=41)).isoformat()  # six weeks keeps the grid readable
    rows = [{"day": d["date"], "status": (d.get("inventory") or {}).get("reservation", "unknown")}
            for d in cal.get("scheduled", []) if today <= d["date"] <= horizon]
    if not rows:
        st.caption("No calendar data.")
        return
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["day"])
    df["week"] = (df["date"] - pd.to_timedelta(df["date"].dt.weekday, unit="D")).dt.strftime("%b %d")
    df["weekday"] = df["date"].dt.strftime("%a")
    df["num"] = df["date"].dt.day.astype(str)
    df["label"] = df["date"].dt.strftime("%a, %b %d")
    df["now"] = df["status"].map({"available": "open now", "sold-out": "sold out", "closed": "closed"}).fillna("sold out")
    df["watching"] = [", ".join(n for n, days, _ in watchers if d in days) or "—" for d in df["day"]]
    df["booked"] = [", ".join(n for n, _, bk in watchers if d in bk) or "—" for d in df["day"]]
    order = list(dict.fromkeys(df.sort_values("date")["week"]))
    fills = STATUS_FILL[mode]
    x = alt.X("weekday:O", sort=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], title=None,
              axis=alt.Axis(orient="top", labelAngle=0, ticks=False, domain=False))
    y = alt.Y("week:O", sort=order, title=None, axis=alt.Axis(ticks=False, domain=False, labelPadding=8))
    tooltip = [alt.Tooltip("label:N", title="Date"), alt.Tooltip("now:N", title="Right now"),
               alt.Tooltip("watching:N", title="Watched by"), alt.Tooltip("booked:N", title="Booked by")]
    cells = alt.Chart(df).mark_rect(cornerRadius=6).encode(
        x=x, y=y, tooltip=tooltip,
        color=alt.Color("now:N", scale=alt.Scale(domain=list(fills), range=list(fills.values())),
                        legend=alt.Legend(title=None, orient="top")))
    ink = alt.condition(alt.datum.now == "closed", alt.value("#0b0b0b" if mode == "light" else "#9a9a96"),
                        alt.value("white"))
    layers = [cells, alt.Chart(df).mark_text(fontSize=13, fontWeight="bold", dy=-8).encode(
        x=x, y=y, text="num:N", color=ink, tooltip=tooltip)]
    # One badge per watching profile under the day number: a dot in the profile's color with a white
    # ring (visible on red and blue cells alike), with a ✓ inside once that date is booked.
    n = len(watchers)
    for i, (name, days, booked) in enumerate(watchers):
        mine = df[df["day"].isin(days | booked)].copy()
        if mine.empty:
            continue
        mine["mark"] = ["✓" if d in booked else "" for d in mine["day"]]
        dx = (i - (n - 1) / 2) * 20
        for size, color in ((22, "white"), (16, profile_color(name_to_profile(name)))):  # white ring, then the dot
            layers.append(alt.Chart(mine).mark_text(fontSize=size, color=color, dx=dx, dy=12, baseline="middle")
                          .encode(x=x, y=y, text=alt.value("●"), tooltip=tooltip))
        layers.append(alt.Chart(mine).mark_text(fontSize=9, fontWeight="bold", color="white", dx=dx, dy=12,
                                                baseline="middle").encode(x=x, y=y, text="mark:N", tooltip=tooltip))
    st.altair_chart(alt.layer(*layers).properties(height=58 * len(order)).configure_scale(bandPaddingInner=0.08)
                    .configure_view(strokeWidth=0), width="stretch")


def name_to_profile(label: str) -> str:
    return next((p for p in list_profiles() if profile_label(p) == label), MAIN)


def dashboard():
    st.markdown(CSS, unsafe_allow_html=True)
    profiles = list_profiles()
    data = {}
    for p in profiles:
        c = load_cfg(p)
        runs = {m: bot_status(m, p) for m in MODES}
        logs = [parse_log(m, p) for m in MODES]
        merged = {"checks": sorted(logs[0]["checks"] + logs[1]["checks"]),
                  "openings": logs[0]["openings"] + logs[1]["openings"],
                  "booked": logs[0]["booked"] + logs[1]["booked"],
                  "errors": logs[0]["errors"] + logs[1]["errors"]}
        data[p] = (c, runs, merged)

    # Celebrate each real booking once per browser session.
    seen = st.session_state.setdefault("_celebrated", set())
    first_look = not st.session_state.setdefault("_dash_seen", False)
    st.session_state["_dash_seen"] = True
    for p, (_, _, m) in data.items():
        for b in m["booked"]:
            key = (p, b["day"], b["time"], b["dry"])
            if key not in seen:
                seen.add(key)
                if not b["dry"] and not first_look:
                    st.balloons()
                    st.toast(f"🎉 {profile_label(p)} booked {b['day']} at {to_12h(b['time'])}!")

    today = dt.date.today()
    k = st.columns(4)
    k[0].metric("Bots running", sum(1 for _, r, _ in data.values() for v in r.values() if v))
    k[1].metric("Checks today", f"{sum(1 for _, _, m in data.values() for t in m['checks'] if t.date() == today):,}")
    k[2].metric("Openings seen", sum(m["openings"] for _, _, m in data.values()))
    k[3].metric("Bookings", sum(1 for _, _, m in data.values() for b in m["booked"] if not b["dry"]))

    per_row = 2 if len(profiles) <= 4 else 3
    for i in range(0, len(profiles), per_row):
        cols = st.columns(per_row)
        for col, p in zip(cols, profiles[i:i + per_row]):
            c, runs, m = data[p]
            with col:
                st.markdown(profile_card_html(p, c, runs, m), unsafe_allow_html=True)
                b1, b2, b3 = st.columns(3)
                for btn_col, mode in ((b1, "watch"), (b2, "snipe")):
                    if runs[mode]:
                        btn_col.button(f"■ Stop {mode}", key=f"d_stop_{p}_{mode}", on_click=stop_bot,
                                       args=(mode, p), width="stretch")
                    elif btn_col.button(f"▶ {mode.title()}", key=f"d_start_{p}_{mode}", type="primary",
                                        width="stretch"):
                        start_saved(mode, p)
                if b3.button("✎ Edit", key=f"d_edit_{p}", width="stretch"):
                    edit_profile(p)
                    st.rerun()  # full-page rerun: the view switch lives outside this fragment

    # Live availability calendar for each restaurant the profiles use.
    venues: dict[tuple[int, int], dict] = {}
    for p in profiles:
        c, _, m = data[p]
        if not c.get("venue_id"):
            continue
        v = venues.setdefault((int(c["venue_id"]), int(c.get("party_size", 2))),
                              {"name": c.get("restaurant_name", "?"), "watchers": []})
        booked = {b["day"] for b in m["booked"] if not b["dry"]}
        v["watchers"].append((profile_label(p), set(c.get("watch_dates") or []), booked))
    if venues:
        st.markdown("##### Calendar")
        keys = list(venues)
        tabs = st.tabs([f"{venues[k]['name']} · party of {k[1]}" for k in keys]) if len(keys) > 1 else [st.container()]
        for tab, k in zip(tabs, keys):
            with tab:
                if len(keys) == 1:
                    st.caption(f"{venues[k]['name']} · party of {k[1]}")
                availability_calendar(k[0], k[1], venues[k]["watchers"], theme_mode())
                legend = " &nbsp; ".join(
                    f'<span style="color:{profile_color(name_to_profile(n))}; font-weight:700">●</span> {html.escape(n)}'
                    for n, _, _ in venues[k]["watchers"])
                st.markdown(f'<div style="font-size:13px; opacity:.85">{legend} &nbsp;·&nbsp; dot = watching &nbsp; ✓ = booked'
                            f' &nbsp;·&nbsp; refreshes every minute</div>', unsafe_allow_html=True)

    st.markdown("##### Activity · last 6 hours")
    series = {profile_label(p): data[p][2]["checks"] for p in profiles}
    if any(series.values()):
        activity_chart(series, [profile_color(p) for p in profiles])
    else:
        st.caption("No checks yet — start a bot and its activity shows up here.")
    errs = [(p, m["errors"]) for p, (_, _, m) in data.items() if m["errors"]]
    if errs:
        st.caption("⚠️ Warnings in logs: " + ", ".join(f"{profile_label(p)} ({n})" for p, n in errs)
                   + " — open ✎ Edit to read the log.")


# ---------------------------------------------------------------------------
# openings tracker (resy_tracker.py) + its charts
# ---------------------------------------------------------------------------

TRACKER = os.path.join(HERE, "resy_tracker.py")
OPENINGS_DB = os.path.join(os.path.dirname(MAIN_CONFIG), "openings.db")
TRACKER_RUN = os.path.join(os.path.dirname(MAIN_CONFIG), ".runs")
SEQ = {"light": ["#cde2fb", "#0d366b"], "dark": ["#1c3350", "#9ec5f4"]}  # one-hue ramp: few -> many openings
CLOSED_FILL = {"light": "#e6e6e3", "dark": "#2a2a28"}


def tracker_status() -> dict | None:
    try:
        with open(os.path.join(TRACKER_RUN, "tracker.json"), "r", encoding="utf-8") as f:
            info = json.load(f)
        proc = psutil.Process(info["pid"])
        if proc.is_running() and "resy_tracker.py" in " ".join(proc.cmdline()):
            return info
    except (OSError, ValueError, KeyError, psutil.Error):
        pass
    return None


def start_tracker():
    os.makedirs(TRACKER_RUN, exist_ok=True)
    log = open(os.path.join(TRACKER_RUN, "tracker.log"), "a", encoding="utf-8")
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    proc = subprocess.Popen([python_exe(), "-u", TRACKER, "--config", MAIN_CONFIG, "--interval", "60"],
                            cwd=HERE, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags)
    log.close()
    with open(os.path.join(TRACKER_RUN, "tracker.json"), "w", encoding="utf-8") as f:
        json.dump({"pid": proc.pid, "started": dt.datetime.now().isoformat(timespec="seconds")}, f)


def stop_tracker():
    info = tracker_status()
    if info:
        try:
            psutil.Process(info["pid"]).terminate()
        except psutil.Error:
            pass


def read_tracker_db():
    """(venues, status, openings) DataFrames, or None if nothing recorded yet."""
    if not os.path.exists(OPENINGS_DB):
        return None
    import sqlite3
    with sqlite3.connect(f"file:{OPENINGS_DB}?mode=ro", uri=True, timeout=10) as conn:
        try:
            venues = pd.read_sql("SELECT * FROM venues", conn)
            status = pd.read_sql("SELECT * FROM status", conn)
            opens = pd.read_sql("SELECT * FROM openings", conn)
            try:
                slots = pd.read_sql("SELECT * FROM slots", conn)
            except Exception:  # database from before slot timing existed
                slots = pd.DataFrame(columns=["id", "opening_id", "time", "seen_at", "gone_at"])
        except Exception:
            return None
    if venues.empty:
        return None
    return venues, status, opens, slots


def fmt_secs(sec: float) -> str:
    if sec != sec:  # NaN
        return "—"
    sec = int(round(sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {sec % 3600 // 60:02d}m"


SPEED_BUCKETS = [(60, "< 1 min"), (300, "1–5 min"), (900, "5–15 min"), (3600, "15–60 min"), (10800, "1–3 h"),
                 (float("inf"), "3 h +")]


def speed_section(o: pd.DataFrame, slots: pd.DataFrame, color: str, mode: str):
    """How fast tables get booked: per time slot after the release, and after cancellations."""
    sl = slots[slots["opening_id"].isin(o["id"])].copy()
    sl = sl.merge(o[["id", "kind", "day", "opened"]], left_on="opening_id", right_on="id", suffixes=("", "_o"))
    sl["seen"] = pd.to_datetime(sl["seen_at"])
    sl["gone"] = pd.to_datetime(sl["gone_at"])
    sl["secs"] = (sl["gone"] - sl["opened"]).dt.total_seconds()     # from the moment the day opened
    sl["held"] = (sl["gone"] - sl["seen"]).dt.total_seconds()       # how long that time stayed bookable

    st.markdown("##### How fast tables get booked")
    left, right = st.columns(2)
    with left:
        rel = o[o["kind"] == "release"].sort_values("opened", ascending=False)
        st.markdown("**⚡ At the daily release**")
        if rel.empty:
            st.caption("No release recorded yet. The tracker checks every 5 seconds around the release time, "
                       "so the next one will show each time slot and how fast it went.")
        else:
            labels = [f"{dt.date.fromisoformat(d):%a, %b} {int(d[8:])} (released {t:%b} {t.day}, {t:%I:%M %p})"
                      for d, t in zip(rel["day"], rel["opened"])]
            pick = st.selectbox("Release", labels, label_visibility="collapsed", key="release_pick")
            r = rel.iloc[labels.index(pick)]
            gone = sl[sl["opening_id"] == r["id"]].sort_values("time")
            sold = (r["closed"] - r["opened"]).total_seconds() if pd.notna(r["closed"]) else float("nan")
            st.metric("Sold out after", fmt_secs(sold) if sold == sold else "still has tables")
            if gone.empty:
                st.caption("No per-time data for this release (a profile token is needed to see times).")
            else:
                g = gone.assign(label=[to_12h(t) for t in gone["time"]],
                                shown=[fmt_secs(x) if x == x else "still open" for x in gone["secs"]],
                                value=gone["secs"].fillna((dt.datetime.now() - r["opened"]).total_seconds()))
                y_axis = alt.Y("label:N", sort=list(g["label"]), title=None,
                               axis=alt.Axis(labelOverlap=False, ticks=False, domain=False))
                bars = alt.Chart(g).mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4, color=color,
                                             height=18).encode(
                    y=y_axis,
                    x=alt.X("value:Q", title="seconds after the release until booked",
                            axis=alt.Axis(gridOpacity=0.15)),
                    tooltip=[alt.Tooltip("label:N", title="Time"), alt.Tooltip("shown:N", title="Booked after")])
                text = alt.Chart(g).mark_text(align="left", dx=5, fontSize=12,
                                              color="#1f2937" if mode == "light" else "#e5e7eb").encode(
                    y=y_axis, x="value:Q", text="shown:N")
                st.altair_chart((bars + text).properties(height=max(120, 32 * len(g))), width="stretch")
            if len(rel) > 1:
                med = sl[sl["kind"] == "release"].groupby("time")["secs"].median().dropna()
                if not med.empty:
                    fastest = med.idxmin()
                    st.caption(f"Across {len(rel)} releases, {to_12h(fastest)} goes fastest "
                               f"(typically {fmt_secs(med[fastest])}).")
    with right:
        st.markdown("**🔁 After a cancellation**")
        cs = sl[(sl["kind"] == "cancellation") & sl["held"].notna()]
        durations = cs["held"] if not cs.empty else             (o[o["kind"] == "cancellation"]["minutes"].dropna() * 60)
        if durations.empty:
            st.caption("No re-booked cancellations yet.")
        else:
            st.metric("Typically re-booked in", fmt_secs(durations.median()))
            names = [n for _, n in SPEED_BUCKETS]
            bucket = [next(n for lim, n in SPEED_BUCKETS if v < lim) for v in durations]
            bdf = pd.DataFrame({"how fast": names,
                                "count": pd.Series(bucket).value_counts().reindex(names, fill_value=0).values})
            simple_bars(bdf, "how fast", "time until someone booked it", color, x_sort=names)
            st.caption("Each freed-up table time, from when it appeared until it was booked again.")


def fmt_minutes(m: float) -> str:
    if m != m:  # NaN
        return "—"
    return f"{m:.0f} min" if m < 90 else f"{m / 60:.1f} h"


def calendar_heatmap(days: pd.DataFrame, mode: str):
    """Every bookable day as a calendar cell, shaded by how many openings were recorded."""
    df = days.copy()
    df["date"] = pd.to_datetime(df["day"])
    df["week"] = (df["date"] - pd.to_timedelta(df["date"].dt.weekday, unit="D")).dt.strftime("%b %d")
    df["weekday"] = df["date"].dt.strftime("%a")
    df["num"] = df["date"].dt.day.astype(str)
    df["label"] = df["date"].dt.strftime("%a, %b %d")
    df["now"] = df["status"].map({"available": "open now", "sold-out": "sold out", "closed": "closed"}).fillna(df["status"])
    df["glyph"] = df["status"].map({"available": "●", "closed": "—"}).fillna("")
    order = list(dict.fromkeys(df.sort_values("date")["week"]))
    top = max(1, int(df["openings"].max()))
    x = alt.X("weekday:O", sort=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], title=None,
              axis=alt.Axis(orient="top", labelAngle=0, ticks=False, domain=False))
    y = alt.Y("week:O", sort=order, title=None, axis=alt.Axis(ticks=False, domain=False, labelPadding=8))
    fill = alt.condition(alt.datum.status == "closed", alt.value(CLOSED_FILL[mode]),
                         alt.Color("openings:Q", scale=alt.Scale(domain=[0, top], range=SEQ[mode]),
                                   legend=alt.Legend(title="openings", orient="right", tickMinStep=1)))
    tooltip = [alt.Tooltip("label:N", title="Date"), alt.Tooltip("now:N", title="Right now"),
               alt.Tooltip("openings:Q", title="Openings recorded"), alt.Tooltip("times:N", title="Times seen"),
               alt.Tooltip("last:N", title="Last opened")]
    cells = alt.Chart(df).mark_rect(cornerRadius=6, stroke="transparent", strokeWidth=2).encode(
        x=x, y=y, color=fill, tooltip=tooltip)
    dark_cell = alt.datum.openings > top / 2 if mode == "light" else alt.datum.openings < top / 2
    ink = alt.condition(dark_cell, alt.value("white" if mode == "light" else "#f0efec"), alt.value("#0b0b0b"))
    nums = alt.Chart(df).mark_text(fontSize=13, fontWeight="bold", dy=-6).encode(x=x, y=y, text="num:N", color=ink,
                                                                                 tooltip=tooltip)
    glyph = alt.Chart(df).mark_text(fontSize=11, dy=10).encode(x=x, y=y, text="glyph:N", color=ink, tooltip=tooltip)
    st.altair_chart((cells + nums + glyph).properties(height=62 * len(order)).configure_scale(bandPaddingInner=0.08),
                    width="stretch")
    st.caption("Shade = openings recorded for that dining date · ● open right now · — closed · hover a day for details.")


def simple_bars(df: pd.DataFrame, x: str, x_title: str, color: str, height: int = 200, x_sort=None):
    chart = alt.Chart(df).mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, color=color).encode(
        x=alt.X(f"{x}:O", title=x_title, sort=x_sort, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("count:Q", title="openings", axis=alt.Axis(tickMinStep=1, gridOpacity=0.15)),
        tooltip=[alt.Tooltip(f"{x}:O", title=x_title), alt.Tooltip("count:Q", title="Openings")])
    st.altair_chart(chart.properties(height=height), width="stretch")


def openings_view():
    mode = theme_mode()
    run = tracker_status()
    head, ctl = st.columns([4, 1], vertical_alignment="center")
    if run:
        since = dt.datetime.fromisoformat(run["started"])
        head.markdown(f"🟢 **Tracker running** · checks every minute · :gray[since {since:%a %b %d, %I:%M %p}]")
        ctl.button("■ Stop tracker", on_click=stop_tracker, width="stretch")
    else:
        head.markdown("⚪ **Tracker off** · :gray[start it to record every opening at your profiles' restaurants]")
        ctl.button("▶ Start tracker", on_click=start_tracker, type="primary", width="stretch")

    data = read_tracker_db()
    if data is None:
        st.info("No data yet. Start the tracker — it records the restaurant and party size of every profile, "
                "checking the whole calendar once a minute. The first openings usually show up within a few hours.",
                icon="📈")
        return
    venues, status, opens, slots = data
    combos = status[["venue_id", "party"]].drop_duplicates().sort_values(["venue_id", "party"])
    names = dict(zip(venues["venue_id"], venues["name"]))
    labels = [f"{names.get(v, v)} · party of {p}" for v, p in combos.itertuples(index=False)]
    pick = st.selectbox("Restaurant", labels, label_visibility="collapsed") if len(labels) > 1 else labels[0]
    v, p = combos.iloc[labels.index(pick)]

    today = dt.date.today().isoformat()
    days = status[(status.venue_id == v) & (status.party == p) & (status.day >= today)].copy()
    o = opens[(opens.venue_id == v) & (opens.party == p)].copy()
    o["opened"] = pd.to_datetime(o["opened_at"])
    o["closed"] = pd.to_datetime(o["closed_at"])
    o["minutes"] = (o["closed"] - o["opened"]).dt.total_seconds() / 60
    cancels = o[o.kind == "cancellation"]

    per_day = cancels.groupby("day").agg(openings=("id", "count"), last=("opened", "max"),
                                         times=("times", lambda s: ", ".join(sorted({t for js in s for t in json.loads(js or "[]")}))))
    days = days.merge(per_day, left_on="day", right_index=True, how="left")
    days["openings"] = days["openings"].fillna(0).astype(int)
    days["times"] = days["times"].fillna("")
    days["last"] = days["last"].dt.strftime("%a %b %d, %I:%M %p").fillna("—")

    first = venues.loc[venues.venue_id == v, "first_poll"].iloc[0]
    hours = max(1.0, (dt.datetime.now() - dt.datetime.fromisoformat(first)).total_seconds() / 3600)
    k = st.columns(4)
    k[0].metric("Cancellations recorded", len(cancels))
    k[1].metric("Per day of tracking", f"{len(cancels) / (hours / 24):.1f}" if hours >= 24 else "—",
                help="Shown once there's a full day of data.")
    k[2].metric("Typical time open", fmt_minutes(cancels["minutes"].median()),
                help="Median minutes between a table opening and getting booked again.")
    since = dt.datetime.fromisoformat(first)
    k[3].metric("Tracking since", f"{since:%b} {since.day}", help=f"{since:%a %b %d, %I:%M %p}")

    st.markdown("##### Every bookable day")
    calendar_heatmap(days, mode)

    blue = SERIES[mode][0]
    a, b = st.columns(2)
    with a:
        st.markdown("##### When cancellations appear")
        by_hour = cancels["opened"].dt.hour.value_counts().reindex(range(24), fill_value=0)
        hdf = pd.DataFrame({"hour": [dt.time(h).strftime("%I %p").lstrip("0") for h in range(24)], "count": by_hour.values})
        simple_bars(hdf, "hour", "time of day", blue, x_sort=list(hdf["hour"]))
    with b:
        st.markdown("##### How far ahead people cancel")
        lead = (pd.to_datetime(cancels["day"]) - cancels["opened"].dt.normalize()).dt.days
        top = int(max(lead.max() if len(lead) else 0, 7))
        ldf = pd.DataFrame({"days before": list(range(top + 1)),
                            "count": lead.value_counts().reindex(range(top + 1), fill_value=0).values})
        simple_bars(ldf, "days before", "days before the dinner", blue)

    speed_section(o, slots, blue, mode)

    st.markdown("##### Opening log")
    if o.empty:
        st.caption("Nothing has opened yet — sold-out days that free up will be listed here.")
    else:
        feed = o.sort_values("opened", ascending=False).head(200)
        st.dataframe(pd.DataFrame({
            "Dining date": pd.to_datetime(feed["day"]).dt.strftime("%a, %b %d"),
            "Type": feed["kind"].map({"cancellation": "Cancellation", "release": "New date released"}),
            "Opened": feed["opened"].dt.strftime("%a %b %d, %I:%M:%S %p"),
            "Lasted": [("still open" if pd.isna(m) else fmt_minutes(m)) for m in feed["minutes"]],
            "Times": [", ".join(json.loads(t or "[]")) or "—" for t in feed["times"]],
        }), hide_index=True, width="stretch")
    st.caption("The tracker sees whether a day has any table. Extra tables on a day that's already open, and "
               "openings re-booked within a minute, won't show up.")


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Resy Bot", page_icon="🍽️", layout="wide")
init_state()
ss = st.session_state
ss.setdefault("view", "📊 Dashboard")
if ss.pop("_goto_settings", False):  # set by a dashboard ✎ Edit click
    ss.view = "⚙️ Settings"
cfg = ss.cfg
info = cfg.get("venue_info") or {}
closed = set(cfg.get("closed_weekdays") or [])
profiles = list_profiles()

with st.sidebar:
    st.header("Profile")

    def profile_option(p: str) -> str:
        running = any(bot_status(m, p) for m in MODES)
        return ("🟢 " if running else "") + profile_label(p)

    st.selectbox("Profile", profiles, key="profile_pick", format_func=profile_option,
                 on_change=switch_profile, label_visibility="collapsed",
                 help="Each profile is one Resy account with its own settings and bots.")
    a, b = st.columns(2)
    with a.popover("➕ New", width="stretch"):
        st.text_input("Name", key="new_profile_name", placeholder="e.g. Alex")
        st.caption("Copies this profile's restaurant, dates and times — not the account token.")
        st.button("Create profile", on_click=create_profile, type="primary", width="stretch")
        if ss.profile_msg:
            st.caption(f":red[{ss.profile_msg}]")
    if current() != MAIN:
        with b.popover("🗑 Delete", width="stretch"):
            busy = any(bot_status(m) for m in MODES)
            st.write(f"Delete **{profile_label(current())}** and its settings?")
            st.button("Delete profile", on_click=delete_profile, disabled=busy, width="stretch")
            if busy:
                st.caption("Stop this profile's bots first.")

    st.divider()
    st.header("Account")
    st.text_input("Profile name", key="profile_name")
    st.text_input("Resy auth token", key="token", type="password",
                  help="DevTools → Network → any api.resy.com request → Request Headers → X-Resy-Auth-Token")
    st.text_input("API key (optional)", key="api_key", type="password",
                  help="Leave blank to use resy.com's public key.")
    st.divider()
    st.header("Booking")
    st.toggle("Dry run (test only)", key="dry_run", help="Runs everything except the final booking.")
    st.toggle("Stop after the first booking", key="stop_first",
              help="Off = keep going until every selected date is booked.")
    if not ss.dry_run:
        st.warning("Live mode: bots will book for real.", icon="⚠️")
    st.divider()
    if st.button("💾 Save settings", width="stretch") and save_settings():
        st.toast("Saved.", icon="✅")
    st.caption("Bots keep running if you close this tab. Reopen the app any time to check on them.")

t1, t2 = st.columns([3, 2], vertical_alignment="center")
t1.title("🍽️ Resy Bot")
t2.segmented_control("View", ["📊 Dashboard", "📈 Openings", "⚙️ Settings"], key="view",
                     label_visibility="collapsed", width="stretch")
view = ss.view or "📊 Dashboard"  # clicking the selected segment again clears it

if view == "📊 Dashboard":
    st.fragment(dashboard, run_every="2s")()
    snapshot_form()
    st.stop()
if view == "📈 Openings":
    st.fragment(openings_view, run_every="15s")()
    snapshot_form()
    st.stop()

# -- settings view: one profile's editor, kept to a readable width ---------------
_, main, _ = st.columns([1, 3, 1])
with main:
    st.caption(f"Editing profile: **{profile_label(current())}** — switch profiles in the sidebar.")

    with st.container(border=True):
        st.subheader("Restaurant")
        c1, c2 = st.columns([5, 1], vertical_alignment="bottom")
        c1.text_input("Resy link", key="url", placeholder="https://resy.com/cities/ny/venues/carbone")
        c2.button("Look up", on_click=lookup_restaurant, width="stretch")
        if ss.lookup_error:
            st.error(f"Lookup failed: {ss.lookup_error}")
        if cfg.get("restaurant_name"):
            where = " · ".join(x for x in (info.get("city"), f"venue {cfg.get('venue_id')}") if x)
            st.markdown(f"#### {cfg['restaurant_name']}  \n:gray[{where}]")
            if info:
                m = st.columns(3)
                m[0].metric("Party size", f"{info['min_party']}–{info['max_party']}")
                m[1].metric("Books ahead", f"{info['days_ahead']} days" if info.get("days_ahead") else "—")
                m[2].metric("Closed", ", ".join(WEEKDAYS[w][:3] for w in sorted(closed)) or "—")
                if info.get("open_days"):
                    st.success("Open tables now: " + ", ".join(nice_date(dt.date.fromisoformat(d)) for d in info["open_days"][:8]))
                else:
                    st.caption(f"No open tables for a party of {cfg.get('party_size', 2)} as of {info.get('checked')}.")
                if not info.get("release_listed"):
                    st.caption("Release time of day isn't listed — set it under Snipe if you know it.")
                if info.get("need_to_know"):
                    with st.expander("Need to know"):
                        st.write(info["need_to_know"])

    with st.container(border=True):
        st.subheader("Reservation")
        lo, hi = info.get("min_party", 1), info.get("max_party", 20)
        ss.party = min(max(int(ss.party), lo), hi)
        st.number_input("Party size", min_value=lo, max_value=hi, step=1, key="party")

        today = dt.date.today()
        options = [today + dt.timedelta(days=i) for i in range(150)]
        options = [d for d in options if d.weekday() not in closed]
        ss.dates = [d for d in ss.dates if d in options]
        st.multiselect("Dates", options=options, key="dates", format_func=nice_date,
                       placeholder="Choose one or more days",
                       help="Closed days are left out." if closed else None)

        st.select_slider("Time window", options=TIME_LABELS, key="window")
        lo_t, hi_t = TIME_LABELS.index(ss.window[0]), TIME_LABELS.index(ss.window[1])
        pref_options = ["No preference"] + TIME_LABELS[lo_t:hi_t + 1]
        if ss.pref not in pref_options:
            ss.pref = "No preference"
        c1, c2 = st.columns(2)
        c1.selectbox("Preferred time", pref_options, key="pref",
                     help="Tried first; otherwise earliest → latest in the window.")
        c2.text_input("Seating type (optional)", key="seating", placeholder="e.g. Dining room",
                      help="Comma-separated. Blank = any seating.")

    st.subheader("Bots")
    w_col, s_col = st.columns(2)
    with w_col.container(border=True):
        st.markdown("**👀 Watch** · catch cancellations")
        st.number_input("Check every (seconds)", min_value=10, max_value=600, step=5, key="poll")
        st.checkbox("Stop at a set time", key="use_stop_at")
        if ss.use_stop_at:
            a, b = st.columns(2)
            a.date_input("Stop date", key="stop_date", label_visibility="collapsed")
            b.time_input("Stop time", key="stop_time", label_visibility="collapsed", step=900)
    with s_col.container(border=True):
        st.markdown("**🎯 Snipe** · grab a date when it opens")
        a, b = st.columns(2)
        a.number_input("Days ahead", min_value=1, max_value=365, step=1, key="days_ahead")
        b.time_input("Opens at", key="release", step=300)
        now = dt.datetime.now()
        for when, d in snipe_plan(st.session_state):  # full run: live widget values
            tag = " · :orange[already open → use Watch]" if when < now else ""
            st.caption(f"{nice_date(d)} → opens {when:%a %b} {when.day}, {when:%I:%M %p}".replace(" 0", " ") + tag)

    @st.fragment(run_every="3s")
    def live_panel():
        cols = st.columns(2)
        for col, mode in zip(cols, MODES):
            with col.container(border=True):
                run = bot_status(mode)
                if run:
                    st.markdown(f"🟢 **{mode.title()} running**  \n" + run_summary(run))
                    st.button(f"Stop {mode}", key=f"stop_{mode}", on_click=stop_bot, args=(mode,),
                              width="stretch")
                else:
                    st.markdown(f"⚪ **{mode.title()}** · not running  \n:gray[uses the settings above]")
                    if st.button(f"Start {mode}", key=f"start_{mode}", type="primary", width="stretch"):
                        if request_start(mode):
                            st.rerun()

        tabs = st.tabs(["Watch log", "Snipe log"])
        for tab, mode in zip(tabs, MODES):
            with tab:
                text = tail(mode)
                st.code(text or "No output yet.", language=None, height=320)

    snapshot_form()
    live_panel()
