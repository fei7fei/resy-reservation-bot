"""👥 Profiles: one card per Resy account, and the editor for a single profile.

The editor saves automatically after every change, so a bot always starts with
what's on screen. Widget keys are prefixed with the profile's slug, so two
profiles' forms never mix.
"""
from __future__ import annotations

import datetime as dt

import segno
import streamlit as st

from resybot import activity, bookings, notify, procs, profiles
from resybot.client import AuthError, ResyClient, booking_policy, parse_venue_url
from ui.common import (MODES, TIME_LABELS, WEEKDAYS, esc, from_12h, log_html, missing_for_start, nice_date,
                       profile_color, request_start, tail_lines, to_12h)

SPEEDS = {"Gentle · every 30s": 30, "Normal · every 15s": 15, "Fast · every 10s": 10}


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------

def add_profile():
    ss = st.session_state
    name = ss.get("new_profile_name", "").strip()
    if not name:
        ss["new_profile_msg"] = "Type a name first."
        return
    try:
        slug = profiles.create(name, copy_from=profiles.MAIN)
    except ValueError as e:
        ss["new_profile_msg"] = str(e)
        return
    ss["new_profile_name"] = ""
    ss["new_profile_msg"] = None
    ss["_open_profile"] = slug


def overview():
    st.title("Profiles")
    st.caption("Each profile is one Resy account with its own restaurant, dates, bots and phone alerts.")
    if slug := st.session_state.pop("_open_profile", None):
        st.query_params["p"] = slug
        st.rerun()
    slugs = profiles.list_profiles()
    cols = st.columns(3)
    for i, s in enumerate(slugs):
        c = profiles.load(s)
        running = [m for m in MODES if procs.bot_status(s, m)]
        expired = activity.profile_stats(s)["token_expired"] and not running
        token = ("🔑 token expired" if expired else "🔑 token saved") if c.get("auth_token") else "🔑 no token yet"
        alerts = "📱 alerts on" if (c.get("alerts") or {}).get("enabled") else "📱 alerts off"
        state = ("🟢 " + " + ".join(running)) if running else "⚪ idle"
        dates = ", ".join(nice_date(d) for d in (c.get("watch_dates") or [])[:3]) or "no dates"
        with cols[i % 3]:
            st.markdown(f"""
<div class="rb-card" style="--rb-c:{profile_color(s)}">
  <div class="rb-head"><div class="rb-avatar">{esc(profiles.label(s))[:1].upper()}</div>
    <div><div class="rb-name">{esc(profiles.label(s))}</div>
    <div class="rb-sub">{esc(c.get("restaurant_name") or "No restaurant yet")} · {esc(dates)}</div></div></div>
  <div class="rb-pills"><span class="rb-pill">{state}</span><span class="rb-pill{' warn' if expired else ''}">{token}</span>
    <span class="rb-pill">{alerts}</span></div>
</div>""", unsafe_allow_html=True)
            if st.button("Open", key=f"open_{s}", width="stretch"):
                st.query_params["p"] = s
                st.rerun()
    with cols[len(slugs) % 3].container(border=True):
        st.markdown("**➕ Add a profile**")
        st.text_input("Name", key="new_profile_name", placeholder="e.g. Alex", label_visibility="collapsed")
        st.button("Create", on_click=add_profile, type="primary", width="stretch")
        st.caption("Starts with your restaurant, dates and times — never your token or alerts.")
        if msg := st.session_state.get("new_profile_msg"):
            st.caption(f":red[{msg}]")


# ---------------------------------------------------------------------------
# editor
# ---------------------------------------------------------------------------

def k(slug: str, name: str) -> str:
    return f"{slug}.{name}"


def init_form(slug: str, c: dict):
    """Fill any missing widget values from the saved profile."""
    today = dt.date.today()
    dates = sorted({dt.date.fromisoformat(str(d)) for d in (c.get("watch_dates") or []) if str(d) >= today.isoformat()})
    stop_at = c.get("watch_stop_at")
    stop_dt = dt.datetime.strptime(stop_at, "%Y-%m-%d %H:%M:%S") if stop_at else dt.datetime.now() + dt.timedelta(days=1)
    poll = int(c.get("watch_poll_interval_seconds", 15))
    a = c.get("alerts") or {}
    values = {
        "name": c.get("profile_name") or profiles.label(slug),
        "url": c.get("restaurant_url", ""),
        "party": int(c.get("party_size", 2)),
        "dates": dates,
        "window": (to_12h(c.get("preferred_time_start", "18:00")), to_12h(c.get("preferred_time_end", "21:00"))),
        "pref": to_12h(c["preferred_time"]) if c.get("preferred_time") else "No preference",
        "seating": ", ".join(c.get("seating_types") or []),
        "speed": min(SPEEDS, key=lambda lbl: abs(SPEEDS[lbl] - poll)),
        "use_stop_at": bool(stop_at),
        "stop_date": stop_dt.date(),
        "stop_time": stop_dt.time().replace(second=0, microsecond=0),
        "days_ahead": int(c.get("snipe_days_ahead", 30)),
        "release": dt.datetime.strptime(c.get("release_time", "09:00:00"), "%H:%M:%S").time(),
        "dry_run": bool(c.get("dry_run", True)),
        "stop_first": bool(c.get("stop_after_first_booking", True)),
        "token": c.get("auth_token", "") or "",
        "alerts_on": bool(a.get("enabled")),
        "alert_booked": bool(a.get("booked", True)),
        "alert_openings": bool(a.get("openings", True)),
        "alert_problems": bool(a.get("problems", True)),
    }
    ss = st.session_state
    for name, v in values.items():
        ss.setdefault(k(slug, name), v)
    w = ss[k(slug, "window")]
    if not (isinstance(w, (tuple, list)) and len(w) == 2 and all(x in TIME_LABELS for x in w)):
        ss[k(slug, "window")] = values["window"]


def autosave(slug: str):
    """Write the form into the profile if anything changed (runs after every widget has rendered)."""
    ss = st.session_state
    disk = profiles.load(slug)
    c = dict(disk)
    v = lambda name: ss[k(slug, name)]  # noqa: E731
    dates = [d.isoformat() for d in sorted(v("dates"))]
    c.update(profile_name=v("name").strip() or profiles.label(slug), party_size=int(v("party")), watch_dates=dates,
             preferred_time_start=from_12h(v("window")[0]), preferred_time_end=from_12h(v("window")[1]),
             watch_poll_interval_seconds=SPEEDS[v("speed")], snipe_days_ahead=int(v("days_ahead")),
             release_time=v("release").strftime("%H:%M:%S"), dry_run=bool(v("dry_run")),
             stop_after_first_booking=bool(v("stop_first")))
    if dates:
        c["reservation_date"] = dates[0]
        first = min(dt.datetime.combine(dt.date.fromisoformat(d) - dt.timedelta(days=c["snipe_days_ahead"]),
                                        v("release")) for d in dates)
        c["drop_at"] = first.strftime("%Y-%m-%d %H:%M:%S")
    if v("pref") != "No preference":
        c["preferred_time"] = from_12h(v("pref"))
    else:
        c.pop("preferred_time", None)
    seating = [x.strip() for x in v("seating").split(",") if x.strip()]
    if seating:
        c["seating_types"] = seating
    else:
        c.pop("seating_types", None)
    if v("use_stop_at"):
        c["watch_stop_at"] = dt.datetime.combine(v("stop_date"), v("stop_time")).strftime("%Y-%m-%d %H:%M:%S")
    else:
        c.pop("watch_stop_at", None)
    if v("token").strip():
        c["auth_token"] = v("token").strip()
    else:
        c.pop("auth_token", None)
    alerts = dict(c.get("alerts") or {})
    alerts.update(enabled=bool(v("alerts_on")), booked=bool(v("alert_booked")), openings=bool(v("alert_openings")),
                  problems=bool(v("alert_problems")))
    if alerts["enabled"] and not alerts.get("topic"):
        alerts["topic"] = notify.new_topic()
    c["alerts"] = alerts
    if c != disk:
        profiles.save(slug, c)


def lookup(slug: str):
    """Look up button: fetch the venue + calendar, save what we learn, and update the form."""
    ss = st.session_state
    ss[k(slug, "lookup_error")] = None
    try:
        location, url_slug = parse_venue_url(ss[k(slug, "url")])
        client = ResyClient()
        venue = client.get_venue(url_slug, location)
        venue_id = venue["id"]["resy"]
        cal = client.get_calendar(venue_id, int(ss[k(slug, "party")]))
    except Exception as e:  # bad link / network / API change -> show on page
        ss[k(slug, "lookup_error")] = str(e)
        return
    policy = booking_policy(venue)
    release = policy.get("release_time") or ss[k(slug, "release")].strftime("%H:%M:%S")
    last_day = cal.get("last_calendar_day")
    days_ahead = None
    if last_day:  # the calendar is ground truth (venue text can be off by one)
        days_ahead = (dt.date.fromisoformat(last_day) - dt.date.today()).days
        if dt.datetime.now().time() < dt.datetime.strptime(release, "%H:%M:%S").time():
            days_ahead += 1  # before today's release, today's new date hasn't unlocked yet
    days_ahead = days_ahead or policy.get("days_ahead")
    statuses: dict[int, set] = {}
    for d in cal.get("scheduled", []):
        statuses.setdefault(dt.date.fromisoformat(d["date"]).weekday(), set()).add((d.get("inventory") or {}).get("reservation"))
    closed = sorted(wd for wd, s in statuses.items() if s == {"closed"})
    c = profiles.load(slug)
    c.update(venue_id=venue_id, restaurant_name=venue.get("name", "?"), restaurant_url=ss[k(slug, "url")].strip(),
             closed_weekdays=closed, release_time=release,
             venue_info={"city": (venue.get("location") or {}).get("locality", ""),
                         "min_party": venue.get("min_party_size") or 1, "max_party": venue.get("max_party_size") or 20,
                         "days_ahead": days_ahead, "release_listed": bool(policy.get("release_time")),
                         "last_day": last_day, "need_to_know": policy.get("text", ""),
                         "checked": dt.datetime.now().strftime("%b %d, %I:%M %p")})
    if days_ahead:
        c["snipe_days_ahead"] = days_ahead
        ss[k(slug, "days_ahead")] = days_ahead
    ss[k(slug, "release")] = dt.datetime.strptime(release, "%H:%M:%S").time()
    lo, hi = c["venue_info"]["min_party"], c["venue_info"]["max_party"]
    ss[k(slug, "party")] = min(max(int(ss[k(slug, "party")]), lo), hi)
    ss[k(slug, "dates")] = [d for d in ss[k(slug, "dates")] if d.weekday() not in closed]
    profiles.save(slug, c)


def check_token(slug: str):
    ss = st.session_state
    token = ss[k(slug, "token")].strip()
    if not token:
        ss[k(slug, "token_result")] = ("bad", "Paste a token first.")
        return
    try:
        c = ResyClient()
        c.set_auth_token(token)
        user = c.check_token()
        cards = len(user.get("payment_methods") or [])
        ss[k(slug, "token_result")] = ("ok", f"Signed in as {user.get('first_name') or 'your account'} · "
                                             f"{cards} card{'s' if cards != 1 else ''} on file")
    except AuthError:
        ss[k(slug, "token_result")] = ("bad", "Resy rejected this token — it's expired or incomplete. Copy a fresh one.")
    except Exception as e:
        ss[k(slug, "token_result")] = ("bad", f"Couldn't check right now: {e}")


def send_test_alert(slug: str):
    c = profiles.load(slug)
    topic = (c.get("alerts") or {}).get("topic")
    ok = notify.send(topic, "✅ Resy Reservation Bot alerts work", f"Alerts for {c.get('profile_name') or slug} are set up.",
                     priority=4, tags=["white_check_mark"])
    st.session_state[k(slug, "alert_test")] = "Sent — check your phone." if ok else "Couldn't reach ntfy.sh right now."


def preview_booking_alert(slug: str):
    """Send exactly what a successful booking looks like, marked as a preview."""
    c = profiles.load(slug)
    topic = (c.get("alerts") or {}).get("topic")
    day = (c.get("watch_dates") or [dt.date.today().isoformat()])[0]
    when = f"{nice_date(day).replace(',', '')} at {to_12h(c.get('preferred_time') or c.get('preferred_time_start', '19:00'))}"
    ok = notify.send(topic, f"🎉 Booked {c.get('restaurant_name') or 'the restaurant'}! (preview)",
                     f"{when} · party of {c.get('party_size', 2)} · on {c.get('profile_name') or slug}'s Resy account. "
                     f"This is a preview — a real booking alert looks exactly like this.",
                     priority=5, tags=["tada"], click="https://resy.com/account")
    st.session_state[k(slug, "alert_test")] = "Preview sent — check your phone." if ok else "Couldn't reach ntfy.sh right now."


def new_topic(slug: str):
    c = profiles.load(slug)
    c["alerts"] = {**(c.get("alerts") or {}), "topic": notify.new_topic()}
    profiles.save(slug, c)


def delete_profile(slug: str):
    profiles.delete(slug)
    st.query_params.clear()


def setup_steps(slug: str, c: dict):
    expired = activity.profile_stats(slug)["token_expired"]
    steps = [("Restaurant", bool(c.get("venue_id"))), ("Dates", bool(c.get("watch_dates"))),
             ("Token", bool(c.get("auth_token")) and not expired),
             ("Test run", any(procs.run_info(slug, m) for m in MODES)),
             ("Phone alerts", bool((c.get("alerts") or {}).get("enabled")))]
    done = sum(ok for _, ok in steps)
    if done < len(steps):
        st.progress(done / len(steps), text=f"Setup {done}/{len(steps)}")
    st.markdown('<div class="rb-steps">' + "".join(
        f'<span class="rb-step{" done" if ok else ""}">{"✓" if ok else "○"} {name}</span>' for name, ok in steps)
        + "</div>", unsafe_allow_html=True)


@st.fragment(run_every="3s")
def bot_panel(slug: str):
    cols = st.columns(2)
    for col, mode, icon in ((cols[0], "watch", "👀"), (cols[1], "snipe", "🎯")):
        with col.container(border=True):
            run = procs.bot_status(slug, mode)
            if run:
                since = dt.datetime.fromisoformat(run["started"])
                kind = ":red[**LIVE**]" if run["live"] else ":blue[dry run]"
                st.markdown(f"🟢 **{icon} {mode.title()} running** · {kind}  \n"
                            f":gray[since {since:%a %I:%M %p} · {', '.join(nice_date(d) for d in run['dates'])}]")
                st.button(f"■ Stop {mode}", key=k(slug, f"stop_{mode}"), on_click=procs.stop_bot, args=(slug, mode),
                          width="stretch")
            else:
                missing = missing_for_start(profiles.load(slug), mode)
                st.markdown(f"⚪ **{icon} {mode.title()}** · not running  \n"
                            + (f":gray[needs {', '.join(missing)}]" if missing else ":gray[ready]"))
                if st.button(f"▶ Start {mode}", key=k(slug, f"start_{mode}"), type="primary", width="stretch",
                             disabled=bool(missing)):
                    if request_start(slug, mode):
                        st.rerun()
    tabs = st.tabs(["Watch log", "Snipe log"])
    for tab, mode in zip(tabs, MODES):
        with tab:
            st.markdown(log_html(tail_lines(procs.log_path(slug, mode), 200)), unsafe_allow_html=True)


def editor(slug: str):
    ss = st.session_state
    c = profiles.load(slug)
    init_form(slug, c)
    info = c.get("venue_info") or {}
    closed = set(c.get("closed_weekdays") or [])

    if st.button("← All profiles"):
        st.query_params.clear()
        st.rerun()
    st.markdown(f"""<div class="rb-head" style="margin:4px 0 8px">
  <div class="rb-avatar" style="--rb-c:{profile_color(slug)}; background:{profile_color(slug)}">{esc(profiles.label(slug))[:1].upper()}</div>
  <div><div class="rb-name" style="font-size:28px">{esc(profiles.label(slug))}</div>
  <div class="rb-sub">Changes save automatically · a running bot keeps the settings it started with</div></div></div>""",
                unsafe_allow_html=True)
    setup_steps(slug, c)

    left, right = st.columns([3, 2], gap="large")
    with left:
        # -- restaurant
        with st.container(border=True):
            st.subheader("🍽️ Restaurant")
            a, b = st.columns([5, 1], vertical_alignment="bottom")
            a.text_input("Resy link", key=k(slug, "url"), placeholder="https://resy.com/cities/ny/venues/carbone")
            b.button("Look up", key=k(slug, "lookup"), on_click=lookup, args=(slug,), width="stretch")
            if err := ss.get(k(slug, "lookup_error")):
                st.error(f"Lookup failed: {err}")
            if c.get("restaurant_name"):
                where = " · ".join(x for x in (info.get("city"), f"venue {c.get('venue_id')}") if x)
                st.markdown(f"#### {c['restaurant_name']}  \n:gray[{where}]")
                if info:
                    m = st.columns(3)
                    m[0].metric("Party size", f"{info['min_party']}–{info['max_party']}")
                    m[1].metric("Books ahead", f"{info['days_ahead']} days" if info.get("days_ahead") else "—")
                    m[2].metric("Closed", ", ".join(WEEKDAYS[w][:3] for w in sorted(closed)) or "—")
                    if info.get("need_to_know"):
                        with st.expander("Need to know"):
                            st.write(info["need_to_know"])

        # -- reservation
        with st.container(border=True):
            st.subheader("📅 Reservation")
            lo, hi = info.get("min_party", 1), info.get("max_party", 20)
            ss[k(slug, "party")] = min(max(int(ss[k(slug, "party")]), lo), hi)
            today = dt.date.today()
            options = [d for d in (today + dt.timedelta(days=i) for i in range(150)) if d.weekday() not in closed]
            ss[k(slug, "dates")] = [d for d in ss[k(slug, "dates")] if d in options]
            a, b = st.columns([1, 3])
            a.number_input("Party size", min_value=lo, max_value=hi, step=1, key=k(slug, "party"))
            b.multiselect("Dates", options=options, key=k(slug, "dates"), format_func=nice_date,
                          placeholder="Choose one or more days", help="Closed days are left out." if closed else None)
            st.select_slider("Time window", options=TIME_LABELS, key=k(slug, "window"))
            lo_t, hi_t = (TIME_LABELS.index(x) for x in ss[k(slug, "window")])
            pref_options = ["No preference"] + TIME_LABELS[lo_t:hi_t + 1]
            if ss[k(slug, "pref")] not in pref_options:
                ss[k(slug, "pref")] = "No preference"
            a, b = st.columns(2)
            a.selectbox("Preferred time", pref_options, key=k(slug, "pref"),
                        help="Tried first; otherwise earliest → latest in the window.")
            b.text_input("Seating type (optional)", key=k(slug, "seating"), placeholder="e.g. Dining room",
                         help="Comma-separated. Blank = any seating.")

        # -- bot settings
        with st.container(border=True):
            st.subheader("🤖 Bots")
            a, b = st.columns(2, gap="large")
            with a:
                st.markdown("**👀 Watch** · catch cancellations")
                st.select_slider("How often to check", options=list(SPEEDS), key=k(slug, "speed"),
                                 help="One calendar request covers all your dates; a date's times are only fetched "
                                      "when it shows a table. Normal ≈ 4 requests a minute — about what a person "
                                      "refreshing the page would do. Fast is fine for a few hours.")
                st.checkbox("Stop at a set time", key=k(slug, "use_stop_at"))
                if ss[k(slug, "use_stop_at")]:
                    x, y = st.columns(2)
                    x.date_input("Stop date", key=k(slug, "stop_date"), label_visibility="collapsed")
                    y.time_input("Stop time", key=k(slug, "stop_time"), label_visibility="collapsed", step=900)
            with b:
                st.markdown("**🎯 Snipe** · grab a date the moment it's released")
                x, y = st.columns(2)
                x.number_input("Days ahead", min_value=1, max_value=365, step=1, key=k(slug, "days_ahead"))
                y.time_input("Opens at", key=k(slug, "release"), step=300)
                now = dt.datetime.now()
                for d in sorted(ss[k(slug, "dates")]):
                    when = dt.datetime.combine(d - dt.timedelta(days=int(ss[k(slug, "days_ahead")])), ss[k(slug, "release")])
                    tag = " · :orange[already open → use Watch]" if when < now else ""
                    st.caption(f"{nice_date(d)} → opens {when:%a %b} {when.day}, {when:%I:%M %p}".replace(" 0", " ") + tag)
            st.divider()
            a, b = st.columns(2)
            a.toggle("Dry run (test only — never books)", key=k(slug, "dry_run"))
            b.toggle("Stop after the first booking", key=k(slug, "stop_first"),
                     help="Off = keep going until every selected date is booked.")
            if not ss[k(slug, "dry_run")]:
                st.warning("Live: bots on this profile will make real bookings with the card on this Resy account.",
                           icon="⚠️")

    with right:
        # -- account
        with st.container(border=True):
            st.subheader("🔑 Account")
            st.text_input("Profile name", key=k(slug, "name"))
            st.text_input("Resy auth token", key=k(slug, "token"), type="password",
                          help="See the Help page for where to find it.")
            st.button("Check token", key=k(slug, "check"), on_click=check_token, args=(slug,))
            if res := ss.get(k(slug, "token_result")):
                (st.success if res[0] == "ok" else st.error)(res[1])
            elif activity.profile_stats(slug)["token_expired"]:
                st.error("The last bot run stopped because this token expired. Paste a fresh one.")

        # -- phone alerts
        with st.container(border=True):
            st.subheader("📱 Phone alerts")
            st.toggle("Send alerts to a phone", key=k(slug, "alerts_on"))
            topic = (c.get("alerts") or {}).get("topic")
            if ss[k(slug, "alerts_on")] and not topic:  # first time on: make this profile's private topic now
                topic = notify.new_topic()
                c["alerts"] = {**(c.get("alerts") or {}), "enabled": True, "topic": topic}
                profiles.save(slug, c)
            if ss[k(slug, "alerts_on")]:
                url = notify.subscribe_url(topic)
                st.markdown("1. Install **ntfy** (free, green bell icon) from the App Store / Google Play\n"
                            "2. Open it and tap **+** (*Subscribe to topic*)\n"
                            "3. Type this topic name exactly — leave the server as **ntfy.sh** — and tap **Subscribe**:")
                st.code(topic, language=None)
                with st.expander("Easier: get the topic onto your phone"):
                    q, t = st.columns([1, 2], vertical_alignment="center")
                    q.markdown(segno.make(url).svg_inline(scale=3, border=2, dark="#111111", light="#ffffff"),
                               unsafe_allow_html=True)
                    t.caption("Point your phone's **camera** at this code. It opens this topic's page, where you can "
                              "copy the name (on Android it may offer to open the ntfy app directly). "
                              "Or text yourself the topic name and copy-paste it.")
                st.markdown("**Alert me when…**")
                st.checkbox("🎉 A booking went through (urgent — breaks through Do Not Disturb)",
                            key=k(slug, "alert_booked"))
                st.checkbox("👀 A table opened but the bot couldn't grab it", key=k(slug, "alert_openings"))
                st.checkbox("⚠️ Something needs attention (token expired, bot stopped, snipe missed)",
                            key=k(slug, "alert_problems"))
                x, y, z = st.columns(3)
                x.button("Send a test alert", key=k(slug, "test_alert"), on_click=send_test_alert, args=(slug,),
                         width="stretch")
                y.button("Preview a booking alert", key=k(slug, "preview_booking"), on_click=preview_booking_alert,
                         args=(slug,), width="stretch")
                with z.popover("New topic", width="stretch"):
                    st.caption("Makes a new private topic; phones subscribed to the old one stop getting alerts.")
                    st.button("Make a new topic", key=k(slug, "new_topic"), on_click=new_topic, args=(slug,))
                if msg := ss.pop(k(slug, "alert_test"), None):
                    st.toast(msg)
                st.caption("The topic name is the only key to these alerts — share it only with this profile's owner.")

        # -- bookings
        mine = bookings.for_profile(slug)
        if mine:
            with st.container(border=True):
                st.subheader("🎉 Bookings")
                for b in sorted(mine, key=lambda b: b["day"], reverse=True):
                    past = b["day"] < dt.date.today().isoformat()
                    st.markdown(f"{'~~' if past else ''}**{b.get('restaurant')}** · {nice_date(b['day'])} at "
                                f"{to_12h(b['time'])} · party of {b.get('party')}{'~~' if past else ''}")

    autosave(slug)

    st.subheader("Run")
    bot_panel(slug)

    if slug != profiles.MAIN:
        with st.expander("Delete this profile"):
            busy = any(procs.bot_status(slug, m) for m in MODES)
            st.write(f"Removes **{esc(profiles.label(slug))}**, its settings, token and bot logs.")
            st.button("Delete profile", key=k(slug, "delete"), on_click=delete_profile, args=(slug,), disabled=busy)
            if busy:
                st.caption("Stop this profile's bots first.")


def render():
    slug = st.query_params.get("p")
    if slug and slug in profiles.list_profiles():
        editor(slug)
    else:
        overview()


render()
