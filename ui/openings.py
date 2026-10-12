"""📈 Openings: every cancellation the tracker has recorded, and how fast tables get booked."""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3

import altair as alt
import pandas as pd
import streamlit as st

from resybot import paths, procs
from ui.common import fmt_secs, theme_mode, to_12h

# Openings are green ("more chances") so they never read as the dashboard's blue = open / red = sold out.
SEQ = {"light": ["#b9e3c4", "#0b6b2e"], "dark": ["#1f5a32", "#8fe3a8"]}  # one-hue ramp: 1 -> many openings
NONE_FILL = {"light": "#e9e9e6", "dark": "#2b2b29"}    # no openings recorded yet
CLOSED_FILL = {"light": "#f6f6f4", "dark": "#1b1b1a"}  # restaurant closed that day
GREEN = {"light": "#16893c", "dark": "#3fbf6a"}         # bar charts on this page


def read_tracker_db():
    """(venues, status, openings) DataFrames, or None if nothing recorded yet."""
    if not os.path.exists(paths.DB):
        return None
    with sqlite3.connect(f"file:{paths.DB}?mode=ro", uri=True, timeout=10) as conn:
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
    df["fill"] = ["closed" if s == "closed" else "none" if n == 0 else "some" for s, n in zip(df["status"], df["openings"])]
    tooltip = [alt.Tooltip("label:N", title="Date"), alt.Tooltip("now:N", title="Right now"),
               alt.Tooltip("openings:Q", title="Openings recorded"), alt.Tooltip("times:N", title="Times seen"),
               alt.Tooltip("last:N", title="Last opened")]
    # Two layers: grey cells (closed / nothing recorded yet) and green cells shaded by how many openings.
    plain = alt.Chart(df[df["fill"] != "some"]).mark_rect(cornerRadius=6).encode(
        x=x, y=y, tooltip=tooltip,
        color=alt.Color("fill:N", scale=alt.Scale(domain=["closed", "none"],
                                                  range=[CLOSED_FILL[mode], NONE_FILL[mode]]), legend=None))
    shaded = alt.Chart(df[df["fill"] == "some"]).mark_rect(cornerRadius=6).encode(
        x=x, y=y, tooltip=tooltip,
        color=alt.Color("openings:Q", scale=alt.Scale(domain=[1, max(top, 2)], range=SEQ[mode]),
                        legend=alt.Legend(title="openings", orient="right", tickMinStep=1)))
    # Light text on the darker greens, dark text everywhere else.
    if mode == "light":
        ink = alt.condition((alt.datum.openings > top / 2) & (alt.datum.openings > 0), alt.value("white"),
                            alt.value("#1f2937"))
    else:
        ink = alt.condition((alt.datum.openings >= top / 2) & (alt.datum.openings > 0), alt.value("#0b0b0b"),
                            alt.value("#e5e7eb"))
    nums = alt.Chart(df).mark_text(fontSize=13, fontWeight="bold", dy=-6).encode(x=x, y=y, text="num:N", color=ink,
                                                                                 tooltip=tooltip)
    glyph = alt.Chart(df).mark_text(fontSize=11, dy=10).encode(x=x, y=y, text="glyph:N", color=ink, tooltip=tooltip)
    chart = alt.layer(plain, shaded, nums, glyph).resolve_scale(color="independent")
    st.altair_chart(chart.properties(height=62 * len(order)).configure_scale(bandPaddingInner=0.08)
                    .configure_view(strokeWidth=0), width="stretch")
    st.caption("Greener = more cancellations recorded for that dinner date · grey = none yet · ● open right now · "
               "— closed · hover a day for details.")


def simple_bars(df: pd.DataFrame, x: str, x_title: str, color: str, height: int = 200, x_sort=None):
    chart = alt.Chart(df).mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, color=color).encode(
        x=alt.X(f"{x}:O", title=x_title, sort=x_sort, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("count:Q", title="openings", axis=alt.Axis(tickMinStep=1, gridOpacity=0.15)),
        tooltip=[alt.Tooltip(f"{x}:O", title=x_title), alt.Tooltip("count:Q", title="Openings")])
    st.altair_chart(chart.properties(height=height), width="stretch")


def hour_and_lead_charts(a, b, cancels: pd.DataFrame, green: str):
    with a:
        st.markdown("##### When cancellations appear")
        by_hour = cancels["opened"].dt.hour.value_counts().reindex(range(24), fill_value=0)
        hdf = pd.DataFrame({"hour": [dt.time(h).strftime("%I %p").lstrip("0") for h in range(24)], "count": by_hour.values})
        simple_bars(hdf, "hour", "time of day", green, x_sort=list(hdf["hour"]))
    with b:
        st.markdown("##### How far ahead people cancel")
        lead = (pd.to_datetime(cancels["day"]) - cancels["opened"].dt.normalize()).dt.days
        top = int(max(lead.max() if len(lead) else 0, 7))
        ldf = pd.DataFrame({"days before": list(range(top + 1)),
                            "count": lead.value_counts().reindex(range(top + 1), fill_value=0).values})
        simple_bars(ldf, "days before", "days before the dinner", green)


@st.fragment(run_every="15s")
def openings_view():
    mode = theme_mode()
    run = procs.tracker_status()
    head, ctl = st.columns([4, 1], vertical_alignment="center")
    if run:
        since = dt.datetime.fromisoformat(run["started"])
        head.markdown(f"🟢 **Tracker running** · checks every minute · :gray[since {since:%a %b %d, %I:%M %p}]")
        ctl.button("■ Stop tracker", on_click=procs.stop_tracker, width="stretch")
    else:
        head.markdown("⚪ **Tracker off** · :gray[start it to record every opening at your profiles' restaurants]")
        ctl.button("▶ Start tracker", on_click=procs.start_tracker, type="primary", width="stretch")

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

    green = GREEN[mode]
    a, b = st.columns(2)
    if cancels.empty:
        a.markdown("##### When cancellations appear")
        a.caption("Shows up after the first recorded cancellation.")
        b.markdown("##### How far ahead people cancel")
        b.caption("Shows up after the first recorded cancellation.")
    else:
        hour_and_lead_charts(a, b, cancels, green)

    speed_section(o, slots, green, mode)

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


def render():
    st.title("Openings")
    st.caption("The tracker records every time a sold-out day frees up at your profiles' restaurants — "
               "including days nobody is watching — and how fast those tables get booked.")
    openings_view()


render()
