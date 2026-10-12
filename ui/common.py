"""Shared UI pieces: formatting, colors, CSS, the status bar, and starting bots safely."""
from __future__ import annotations

import datetime as dt
import html

import streamlit as st

from resybot import activity, procs, profiles
from resybot.client import ResyClient

MODES = procs.MODES
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
TIME_VALUES = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 15, 30, 45)]

# Categorical slots in fixed order (validated reference palette), one per profile.
SERIES = {"light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"],
          "dark": ["#3987e5", "#d95926", "#199e70", "#c98500"]}


# -- formatting ------------------------------------------------------------------

def to_12h(hhmm: str) -> str:
    return dt.datetime.strptime(hhmm[:5], "%H:%M").strftime("%I:%M %p").lstrip("0")


def from_12h(label: str) -> str:
    return dt.datetime.strptime(label, "%I:%M %p").strftime("%H:%M")


TIME_LABELS = [to_12h(t) for t in TIME_VALUES]


def nice_date(d: dt.date | str) -> str:
    d = dt.date.fromisoformat(d) if isinstance(d, str) else d
    return f"{d:%a, %b} {d.day}"


def fmt_secs(sec: float) -> str:
    if sec != sec:  # NaN
        return "—"
    sec = int(round(sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {sec % 3600 // 60:02d}m"


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


def esc(text: str) -> str:
    return html.escape(str(text))


# -- colors ----------------------------------------------------------------------

def theme_mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:
        return "light"


def profile_color(slug: str) -> str:
    slugs = profiles.list_profiles()
    return SERIES[theme_mode()][slugs.index(slug) % 4 if slug in slugs else 0]


# -- profile helpers -------------------------------------------------------------

def next_release(cfg: dict) -> dt.datetime | None:
    if cfg.get("snipe_days_ahead") is None or not cfg.get("watch_dates"):
        return None
    rel = dt.datetime.strptime(cfg.get("release_time", "09:00:00"), "%H:%M:%S").time()
    upcoming = [dt.datetime.combine(dt.date.fromisoformat(d) - dt.timedelta(days=int(cfg["snipe_days_ahead"])), rel)
                for d in cfg["watch_dates"]]
    upcoming = [u for u in upcoming if u > dt.datetime.now()]
    return min(upcoming) if upcoming else None


def missing_for_start(cfg: dict, mode: str) -> list[str]:
    missing = [label for key, label in (("venue_id", "a restaurant"), ("watch_dates", "at least one date"),
                                        ("auth_token", "a Resy token")) if not cfg.get(key)]
    if not missing and mode == "snipe" and not next_release(cfg):
        missing.append("a date that hasn't opened yet (all selected dates are already released — use Watch)")
    return missing


@st.dialog("Start a LIVE bot?")
def confirm_live(slug: str, mode: str, cfg: dict):
    who = cfg.get("profile_name") or "this profile"
    st.write(f"Dry run is **off**. The {mode} bot will make a **real booking** on **{esc(who)}**'s Resy account "
             f"(card on file) for:")
    st.write(", ".join(nice_date(d) for d in cfg["watch_dates"]))
    a, b = st.columns(2)
    if a.button("Yes, start it", type="primary", width="stretch"):
        procs.start_bot(slug, mode, cfg)
        st.rerun()
    if b.button("Cancel", width="stretch"):
        st.rerun()


def request_start(slug: str, mode: str) -> bool:
    """Start a profile's bot from its saved settings. True if started now (dry run); live runs ask first."""
    cfg = profiles.load(slug)
    missing = missing_for_start(cfg, mode)
    if missing:
        st.toast(f"{profiles.label(slug)} needs {', '.join(missing)}.", icon="⚠️")
        return False
    if cfg.get("dry_run", True):
        procs.start_bot(slug, mode, cfg)
        return True
    confirm_live(slug, mode, cfg)
    return False


@st.cache_data(ttl=60, show_spinner=False)
def live_calendar(venue_id: int, party: int) -> dict:
    """Resy's availability calendar for a venue (no login needed); cached for a minute."""
    return ResyClient().get_calendar(venue_id, party, days=60)


# -- look ------------------------------------------------------------------------

CSS = """
<style>
/* layout */
.block-container {padding-top: 3.2rem;}
h1, h2, h3, h4, h5 {letter-spacing: -0.01em;}

/* page links styled as secondary buttons (dashboard "Edit") */
[data-testid="stPageLink-NavLink"] {justify-content:center; border:1px solid rgba(127,127,127,.35); border-radius:8px;
  padding: 0.38rem 0.75rem; min-height: 2.5rem; background: transparent;}
[data-testid="stPageLink-NavLink"]:hover {border-color: var(--primary-color, #5b5bd6);}

/* status bar */
.rb-status {display:flex; flex-wrap:wrap; gap:8px; margin: 2px 0 14px;}
.rb-chipbar {display:inline-flex; align-items:center; gap:7px; padding:5px 12px; border-radius:999px; font-size:13px;
  font-weight:600; border:1px solid rgba(127,127,127,.28); background: rgba(127,127,127,.06);}
.rb-chipbar.good {border-color: rgba(12,163,12,.55); background: rgba(12,163,12,.10);}
.rb-chipbar.warn {border-color: rgba(208,59,59,.6); background: rgba(208,59,59,.12);}
.rb-mono {font-family: ui-monospace, Consolas, monospace;}

/* profile cards */
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
.rb-pill.warn {border-color: rgba(208,59,59,.65); background: rgba(208,59,59,.14);}
.rb-dot {width:8px; height:8px; border-radius:50%; background:#0ca30c; animation: rb-pulse 1.6s infinite; flex:none;}
.rb-dot.off {background: rgba(127,127,127,.6); animation:none;}
@keyframes rb-pulse {0%{box-shadow:0 0 0 0 rgba(12,163,12,.55)} 70%{box-shadow:0 0 0 8px rgba(12,163,12,0)} 100%{box-shadow:0 0 0 0 rgba(12,163,12,0)}}
.rb-chips {display:flex; flex-wrap:wrap; gap:6px; margin:6px 0 10px;}
.rb-chip {padding:3px 9px; border-radius:8px; font-size:12.5px; background: rgba(127,127,127,.13);}
.rb-chip.booked {background: rgba(12,163,12,.2); font-weight:700;}
.rb-stats {display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-top:4px;}
.rb-stat b {display:block; font-size:22px; line-height:1.2;}
.rb-stat span {opacity:.68; font-size:12px;}
.rb-booked {margin-top:10px; padding:8px 12px; border-radius:10px; background: rgba(12,163,12,.16);
  border:1px solid rgba(12,163,12,.5); font-weight:700;}

/* activity feed */
.rb-feed {display:flex; flex-direction:column; gap:2px;}
.rb-ev {display:grid; grid-template-columns: 26px 1fr auto; gap:8px; align-items:baseline; padding:7px 10px;
  border-radius:10px; font-size:13.5px;}
.rb-ev:nth-child(odd) {background: rgba(127,127,127,.06);}
.rb-ev .t {opacity:.6; font-size:12px; white-space:nowrap;}
.rb-ev.booked {background: rgba(12,163,12,.14);}
.rb-ev.problem {background: rgba(208,59,59,.12);}

/* setup steps */
.rb-steps {display:flex; flex-wrap:wrap; gap:6px; margin: 4px 0 6px;}
.rb-step {padding:4px 10px; border-radius:999px; font-size:12.5px; border:1px solid rgba(127,127,127,.3);}
.rb-step.done {border-color: rgba(12,163,12,.55); background: rgba(12,163,12,.10);}

/* logs */
.rb-log {font-family: ui-monospace, Consolas, monospace; font-size:12.5px; line-height:1.55; max-height: 560px;
  overflow:auto; padding:10px 12px; border-radius:12px; border:1px solid rgba(127,127,127,.25);}
.rb-log .w {color:#d97706;} .rb-log .e {color:#dc2626; font-weight:600;} .rb-log .i {}
.rb-log .d {opacity:.55;} .rb-log .hl {background: rgba(12,163,12,.16); font-weight:700;}
</style>
"""


def inject_css():
    st.markdown(CSS, unsafe_allow_html=True)


def status_bar():
    """One line that says whether everything's OK, shown on every page."""
    slugs = profiles.list_profiles()
    running = [(s, m, r) for s in slugs for m in MODES if (r := procs.bot_status(s, m))]
    chips = []
    if running:
        live = any(r["live"] for _, _, r in running)
        chips.append(f'<span class="rb-chipbar good"><span class="rb-dot"></span>{len(running)} bot'
                     f'{"s" if len(running) != 1 else ""} running{" · LIVE" if live else " · dry run"}</span>')
    else:
        chips.append('<span class="rb-chipbar"><span class="rb-dot off"></span>No bots running</span>')
    chips.append('<span class="rb-chipbar good">📈 Tracker on</span>' if procs.tracker_status()
                 else '<span class="rb-chipbar">📈 Tracker off</span>')
    drops = [(next_release(profiles.load(s)), s) for s, m, _ in running if m == "snipe"]
    drops = [(t, s) for t, s in drops if t]
    if drops:
        t, s = min(drops)
        chips.append(f'<span class="rb-chipbar">⏱ next drop in <span class="rb-mono">{countdown(t)}</span>'
                     f' · {esc(profiles.label(s))}</span>')
    expired = [profiles.label(s) for s in slugs if not any(procs.bot_status(s, m) for m in MODES)
               and activity.profile_stats(s)["token_expired"]]
    if expired:
        chips.append(f'<span class="rb-chipbar warn">🔑 Token expired: {esc(", ".join(expired))}</span>')
    alerts_on = sum(1 for s in slugs if (profiles.load(s).get("alerts") or {}).get("enabled"))
    chips.append(f'<span class="rb-chipbar{" good" if alerts_on else ""}">📱 Phone alerts: {alerts_on}/{len(slugs)}</span>')
    st.markdown(f'<div class="rb-status">{"".join(chips)}</div>', unsafe_allow_html=True)


# -- logs ------------------------------------------------------------------------

_LEVEL_CLASS = {"DEBUG": "d", "INFO": "i", "WARNING": "w", "ERROR": "e", "CRITICAL": "e"}
_HIGHLIGHT = ("*** Booked", "*** DRY RUN", "Opening spotted", "OPENED", "TOKEN EXPIRED")


def tail_lines(path: str, n: int = 400) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]
    except FileNotFoundError:
        return []


def log_html(lines: list[str], query: str = "", hide_debug: bool = True) -> str:
    """Color-coded log: warnings amber, errors red, bookings/openings highlighted, request noise dimmed."""
    out = []
    for line in lines:
        line = line.rstrip("\n")
        if query and query.lower() not in line.lower():
            continue
        level = next((lv for lv in _LEVEL_CLASS if f"[{lv}]" in line), "INFO")
        if hide_debug and level == "DEBUG":
            continue
        cls = "hl" if any(h in line for h in _HIGHLIGHT) else _LEVEL_CLASS[level]
        out.append(f'<div class="{cls}">{esc(line)}</div>')
    body = "".join(reversed(out)) or '<div class="d">Nothing to show yet.</div>'
    return f'<div class="rb-log">{body}</div>'
