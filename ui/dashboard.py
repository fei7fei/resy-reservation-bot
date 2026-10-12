"""📊 Dashboard: every profile at a glance, the live calendar, and what's been happening."""
from __future__ import annotations

import datetime as dt

import altair as alt
import pandas as pd
import streamlit as st

from resybot import activity, bookings, procs, profiles
from ui.common import (MODES, ago, countdown, esc, live_calendar, nice_date, next_release, profile_color,
                       request_start, theme_mode, to_12h)

STATUS_FILL = {"light": {"open now": "#2a78d6", "sold out": "#d03b3b", "closed": "#e6e6e3"},
               "dark": {"open now": "#3987e5", "sold out": "#c03535", "closed": "#2a2a28"}}


def card_html(slug: str, c: dict, runs: dict, stats: dict, booked: list[dict]) -> str:
    name = esc(profiles.label(slug))
    rest = esc(c.get("restaurant_name") or "No restaurant yet")
    win = f" · {to_12h(c['preferred_time_start'])}–{to_12h(c['preferred_time_end'])}" if c.get("preferred_time_start") else ""
    pills = [f'<span class="rb-pill on"><span class="rb-dot"></span>{icon} {verb}</span>'
             for mode, icon, verb in (("watch", "👀", "Watching"), ("snipe", "🎯", "Snipe armed")) if runs.get(mode)]
    if not pills:
        pills.append('<span class="rb-pill"><span class="rb-dot off"></span>Idle</span>')
    if any(runs.values()):
        live = any(r["live"] for r in runs.values() if r)
        pills.append('<span class="rb-pill live">● LIVE</span>' if live else '<span class="rb-pill">🧪 Dry run</span>')
    nxt = next_release(c)
    if runs.get("snipe") and nxt:
        pills.append(f'<span class="rb-pill">⏱ next drop in&nbsp;<span class="rb-mono">{countdown(nxt)}</span></span>')
    if stats["token_expired"] and not any(runs.values()):
        pills.append('<span class="rb-pill warn">🔑 Token expired — paste a new one</span>')
    if (c.get("alerts") or {}).get("enabled"):
        pills.append('<span class="rb-pill">📱 Alerts on</span>')

    booked_days = {b["day"] for b in booked}
    chips = "".join(f'<span class="rb-chip{" booked" if d in booked_days else ""}">'
                    f'{"✓ " if d in booked_days else ""}{nice_date(d)}</span>' for d in (c.get("watch_dates") or []))
    banner = ""
    if booked:
        b = sorted(booked, key=lambda x: x["day"])[0]
        more = f" (+{len(booked) - 1} more)" if len(booked) > 1 else ""
        banner = (f'<div class="rb-booked">🎉 Booked {esc(b.get("restaurant") or "")} · {nice_date(b["day"])} at '
                  f'{to_12h(b["time"])}{more}</div>')
    elif stats["booked"]:
        b = stats["booked"][-1]
        banner = f'<div class="rb-chip" style="margin-top:8px">🧪 Dry-run match: {nice_date(b["day"])} at {to_12h(b["time"])}</div>'
    last = stats["checks"][-1] if stats["checks"] else None
    return f"""
<div class="rb-card" style="--rb-c:{profile_color(slug)}">
  <div class="rb-head"><div class="rb-avatar">{name[:1].upper()}</div>
    <div><div class="rb-name">{name}</div><div class="rb-sub">{rest} · party of {c.get("party_size", 2)}{win}</div></div></div>
  <div class="rb-pills">{''.join(pills)}</div>
  <div class="rb-chips">{chips or '<span class="rb-chip">No dates picked</span>'}</div>
  <div class="rb-stats">
    <div class="rb-stat"><b>{len(stats['checks']):,}</b><span>checks</span></div>
    <div class="rb-stat"><b>{ago(last)}</b><span>last check</span></div>
    <div class="rb-stat"><b>{stats['openings']}</b><span>tables spotted</span></div>
  </div>
  {banner}
</div>"""


def availability_calendar(venue_id: int, party: int, watchers: list[tuple[str, str, set, set]], mode: str):
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
    df["watching"] = [", ".join(n for _, n, days, _ in watchers if d in days) or "—" for d in df["day"]]
    df["booked"] = [", ".join(n for _, n, _, bk in watchers if d in bk) or "—" for d in df["day"]]
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
    # One badge per profile under the day number: a dot in the profile's color with a white ring
    # (visible on red and blue cells alike), with a ✓ inside once that date is booked.
    n = len(watchers)
    for i, (slug, _, days, booked) in enumerate(watchers):
        mine = df[df["day"].isin(days | booked)].copy()
        if mine.empty:
            continue
        mine["mark"] = ["✓" if d in booked else "" for d in mine["day"]]
        dx = (i - (n - 1) / 2) * 20
        for size, color in ((22, "white"), (16, profile_color(slug))):
            layers.append(alt.Chart(mine).mark_text(fontSize=size, color=color, dx=dx, dy=11, baseline="middle")
                          .encode(x=x, y=y, text=alt.value("●"), tooltip=tooltip))
        layers.append(alt.Chart(mine).mark_text(fontSize=9, fontWeight="bold", color="white", dx=dx, dy=12,
                                                baseline="middle").encode(x=x, y=y, text="mark:N", tooltip=tooltip))
    st.altair_chart(alt.layer(*layers).properties(height=64 * len(order)).configure_scale(bandPaddingInner=0.08)
                    .configure_view(strokeWidth=0), width="stretch")
    legend = " &nbsp; ".join(f'<span style="color:{profile_color(s)}; font-weight:700">●</span> {esc(n)}'
                             for s, n, _, _ in watchers)
    st.markdown(f'<div style="font-size:13px; opacity:.85">{legend} &nbsp;·&nbsp; dot = watching &nbsp; ✓ = booked'
                f' &nbsp;·&nbsp; updates every minute</div>', unsafe_allow_html=True)


def feed(events: list[dict], limit: int = 14):
    if not events:
        st.caption("Nothing yet — bot starts, tables spotted, bookings and tracker finds show up here.")
        return
    rows = []
    for e in sorted(events, key=lambda e: e["when"], reverse=True)[:limit]:
        when = e["when"]
        t = f"{when:%I:%M %p}".lstrip("0") if when.date() == dt.date.today() else f"{when:%b} {when.day}, {when:%I:%M %p}"
        rows.append(f'<div class="rb-ev {e["kind"]}"><span>{e["icon"]}</span><span>{esc(e["text"])}</span>'
                    f'<span class="t">{t}</span></div>')
    st.markdown(f'<div class="rb-feed">{"".join(rows)}</div>', unsafe_allow_html=True)


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
    st.altair_chart((lines + points + rule).properties(height=210), width="stretch")


@st.fragment(run_every="2s")
def live():
    slugs = profiles.list_profiles()
    data = {}
    for s in slugs:
        c = profiles.load(s)
        data[s] = (c, {m: procs.bot_status(s, m) for m in MODES}, activity.profile_stats(s),
                   bookings.for_profile(s, upcoming_only=True))

    # Celebrate each new real booking once per browser session.
    seen = st.session_state.setdefault("_celebrated", set())
    first_look = not st.session_state.get("_dash_seen")
    st.session_state["_dash_seen"] = True
    for s, (_, _, _, booked) in data.items():
        for b in booked:
            key = (s, b["day"], b["time"])
            if key not in seen:
                seen.add(key)
                if not first_look:
                    st.balloons()
                    st.toast(f"🎉 {profiles.label(s)} booked {nice_date(b['day'])} at {to_12h(b['time'])}!")

    today = dt.date.today()
    k = st.columns(4)
    k[0].metric("Bots running", sum(1 for _, r, _, _ in data.values() for v in r.values() if v))
    k[1].metric("Checks today", f"{sum(1 for _, _, m, _ in data.values() for t in m['checks'] if t.date() == today):,}")
    k[2].metric("Tables spotted", sum(m["openings"] for _, _, m, _ in data.values()),
                help="Times a bot found a table inside its time window (latest run of each bot).")
    k[3].metric("Upcoming bookings", sum(len(b) for _, _, _, b in data.values()))

    per_row = 2 if len(slugs) <= 4 else 3
    for i in range(0, len(slugs), per_row):
        cols = st.columns(per_row)
        for col, s in zip(cols, slugs[i:i + per_row]):
            c, runs, m, booked = data[s]
            with col:
                st.markdown(card_html(s, c, runs, m, booked), unsafe_allow_html=True)
                b1, b2, b3 = st.columns(3)
                for btn, mode in ((b1, "watch"), (b2, "snipe")):
                    if runs[mode]:
                        btn.button(f"■ Stop {mode}", key=f"d_stop_{s}_{mode}", on_click=procs.stop_bot,
                                   args=(s, mode), width="stretch")
                    elif btn.button(f"▶ {mode.title()}", key=f"d_start_{s}_{mode}", type="primary", width="stretch"):
                        request_start(s, mode)
                b3.page_link("ui/profiles.py", label="Edit", icon="✏️", query_params={"p": s}, width="stretch")

    left, right = st.columns([3, 2], gap="large")
    with left:
        venues: dict[tuple[int, int], dict] = {}
        for s in slugs:
            c, _, m, booked = data[s]
            if not c.get("venue_id"):
                continue
            v = venues.setdefault((int(c["venue_id"]), int(c.get("party_size", 2))),
                                  {"name": c.get("restaurant_name", "?"), "watchers": []})
            v["watchers"].append((s, profiles.label(s), set(c.get("watch_dates") or []), {b["day"] for b in booked}))
        st.markdown("##### 📅 Calendar")
        if not venues:
            st.caption("Add a restaurant to a profile to see its calendar.")
        keys = list(venues)
        tabs = st.tabs([f"{venues[k]['name']} · party of {k[1]}" for k in keys]) if len(keys) > 1 else [st.container()]
        for tab, key in zip(tabs, keys):
            with tab:
                if len(keys) == 1:
                    st.caption(f"{venues[key]['name']} · party of {key[1]}")
                availability_calendar(key[0], key[1], venues[key]["watchers"], theme_mode())
    with right:
        st.markdown("##### ⚡ Activity")
        events = activity.tracker_events(30)
        for s in slugs:
            events += activity.bot_events(s, profiles.label(s))
        for b in bookings.all_bookings():
            events.append({"when": dt.datetime.fromisoformat(b["booked_at"]), "icon": "🎉", "kind": "booked",
                           "text": f"{b.get('who')} booked {b.get('restaurant')} · {nice_date(b['day'])} at {to_12h(b['time'])}"})
        # A bot's own "booked" log line duplicates the permanent record above.
        events = [e for e in events if not (e["icon"] == "🎉" and "booked 20" in e["text"])]
        feed(events)

    st.markdown("##### Checks · last 6 hours")
    series = {profiles.label(s): data[s][2]["checks"] for s in slugs}
    if any(series.values()):
        activity_chart(series, [profile_color(s) for s in slugs])
    else:
        st.caption("No checks yet — start a bot and its activity shows up here.")


def render():
    st.title("Dashboard")
    live()


render()
