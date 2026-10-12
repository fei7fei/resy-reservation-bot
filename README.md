# Resy Bot

Books hard-to-get Resy tables for you and your friends: it catches cancellations,
grabs new dates the second they're released, and tracks how often tables free up.

Open it with the **Resy Bot** desktop shortcut (or double-click `launch_app.pyw`).
It runs a small local web app at `http://localhost:8501` — only this PC can open it.

## Pages

| Page | What it's for |
|---|---|
| 📊 **Dashboard** | Every profile at a glance, start/stop bots, live availability calendar, activity feed |
| 📈 **Openings** | The cancellation tracker: every opening recorded, when people cancel, how fast tables get booked |
| 👥 **Profiles** | One per Resy account: restaurant, dates, times, token, phone alerts, bots and their logs |
| 📜 **Logs** | Every bot's and the tracker's output, color-coded and searchable |
| ❓ **Help** | Getting a Resy token, setting up phone alerts, how the bots behave |

## The bots

- **👀 Watch** catches cancellations. Each check is one calendar request covering all your
  dates; a date's times are only fetched when it shows a table. Normal speed is about four
  requests a minute, and it backs off automatically if Resy ever says to slow down.
- **🎯 Snipe** waits for the daily release (e.g. 9:00 AM, 29 days ahead) and checks rapidly for
  about a minute. It re-checks the login token five minutes before.
- **📈 Tracker** records every opening at your profiles' restaurants — even dates nobody watches —
  and checks every 5 seconds around the release to time how fast each table goes.

Bots run in the background, keep the PC awake, and keep running if you close the browser tab.
**Dry run** does everything except the final booking.

## Phone alerts

Free via the [ntfy](https://ntfy.sh) app (no account). Turn alerts on in a profile, scan the QR
code with the ntfy app, and press **Send a test alert**. You get bookings, tables that opened but
were taken first, and problems such as an expired token.

## Files

```
app.py              the web app (Streamlit) -- pages are in ui/
resybot/            bots, tracker, Resy client, alerts, profile storage
data/               YOUR DATA (not in git): profiles with Resy tokens, bot logs,
                    tracker database, bookings history
launch_app.pyw      starts the app in the background and opens the browser
```

Keep `data/` private — profile files contain Resy login tokens.

## Notes

- This uses Resy's private website API, not an official one. It can change without notice,
  and automated booking may not be something Resy's terms allow — use it for your own
  reservations at a polite pace.
- Real bookings use the first card saved on that Resy account.

## Development

```
pip install -r requirements.txt
streamlit run app.py
```

Set `RESY_DATA=<folder>` to run against a separate data folder (useful for testing without
touching real profiles).
