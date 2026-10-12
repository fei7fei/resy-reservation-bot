# 🍽️ Resy Reservation Bot

**Automatically books hard-to-get tables on [Resy](https://resy.com) for you.**
It runs on your own computer, with a dashboard in your browser.

> **Unofficial.** Resy Reservation Bot uses the same private web API as resy.com. It isn't
> affiliated with Resy, the API can change without notice, and automated booking may go against
> Resy's terms. Use it for your own reservations, at a polite pace, at your own risk.

---

## Two main functions

### 👀 1. Watch for cancellations
Popular restaurants are "sold out", but people cancel all the time. Pick the dates and time window
you want. The bot checks Resy about every 15 seconds and **books a cancelled table the moment one
appears** in your window. One request covers all your dates, so it stays light.

> *Use it for:* dates that are already fully booked.

### 🎯 2. Snipe new releases
Many restaurants release tables at a fixed time each day, for example *9:00 AM, 29 days ahead*, and
they're gone within seconds. The bot waits for that exact moment and **books in the first seconds
after the release**, faster than you could by hand.

> *Use it for:* dates that haven't opened for booking yet.

You can run both at once. Booking rules you control: party size, one or more dates, earliest and
latest time, a preferred time (tried first), seating type (for example *Dining room* only), and
*stop after the first booking*. **Dry run** mode does everything except the final booking, so you
can test safely.

---

## Also included

| | |
|---|---|
| 📱 **Phone alerts** | Free push notifications through the ntfy app: 🎉 booked, 👀 a table appeared but was taken, ⚠️ a problem such as an expired login. |
| 📈 **Openings tracker** | Records every cancellation at your restaurants, including dates you aren't watching. It also times how fast tables get booked at release and after a cancellation. |
| 👥 **Profiles** | Run bots for several Resy accounts, for example yours and a friend's, each with its own dates, alerts and bookings. |
| 📊 **Dashboard** | Live status of every bot, an availability calendar, an activity feed and charts. |

---

## Requirements

- **Windows 10/11** recommended. It also runs on macOS and Linux, but the double-click launcher,
  keep-awake and beep features are Windows-only.
- **Python 3.10+** from [python.org](https://www.python.org/downloads/). Tick *Add Python to PATH* when installing.
- A **Resy account**, plus a card saved on it if the restaurant requires one.

## Install

```bash
git clone https://github.com/fei7fei/resy-reservation-bot.git
cd resy-reservation-bot
pip install -r requirements.txt
```

## Start it

- **Windows:** double-click **`launch_app.pyw`**. It starts the app in the background and opens
  your browser at <http://localhost:8501>.
  - Want a desktop shortcut? Right-click `launch_app.pyw` → **Send to → Desktop (create shortcut)**.
- **Any system:** `streamlit run app.py`

The app only accepts connections from your own computer, because it holds your Resy login.

---

## First-time setup (about 5 minutes)

### 1. Add a restaurant
Open **👥 Profiles → Me**. Paste the restaurant's resy.com link (for example
`https://resy.com/cities/new-york-ny/venues/carbone`) and press **Look up**. The app fills in the
party-size limits, how many days ahead it books, its closed days, and the daily release time if the restaurant lists one.

### 2. Pick dates and times
Choose one or more **dates**, drag the **time window**, and optionally pick a **preferred time**.

### 3. Connect your Resy account
The bot books with your account's login token, a long code your browser uses once you're signed in.
You copy it once:

1. On a computer, sign in at **resy.com**.
2. Press **F12** (or right-click → *Inspect*) → **Network** tab → press **F5** to reload.
3. Type `api.resy.com` in the filter box and click a request such as **user**.
4. Under **Headers → Request Headers**, find **`X-Resy-Auth-Token`** → right-click → **Copy value**.
5. Paste it into the profile's **Resy auth token** box and press **Check token**.
   You should see ✅ *Signed in as …*

The **❓ Help** page in the app has steps for Safari and Firefox, plus fixes for common problems.
Treat the token like a password.

### 4. Turn on phone alerts (optional, recommended)
1. Install **ntfy** (free; App Store / Google Play).
2. In the profile, switch on **Send alerts to a phone** and copy the topic name it shows.
3. In ntfy, tap **+**, enter that topic (server: `ntfy.sh`), and subscribe.
4. Press **Send a test alert** and **Preview a booking alert**.

### 5. Test, then go live
1. Leave **Dry run** on and press **▶ Start watch**. The log should show it checking.
2. When you're happy, turn **Dry run** off and start the bot again. Real bookings use the first card
   saved on the Resy account.

---

## Everyday use

| To… | Do this |
|---|---|
| Catch cancellations | Pick dates → **▶ Watch** |
| Get a table the moment it's released | Pick dates that haven't opened yet → **▶ Snipe**. The plan shows when each date opens. |
| See how often a restaurant gets cancellations | **📈 Openings → ▶ Start tracker**, then check back over a few days |
| Add a friend | **👥 Profiles → Add a profile**. They connect *their own* Resy account and subscribe to *their own* alert topic. |
| Check what a bot is doing | **📜 Logs**, or the log under each profile |

Bots keep running if you close the browser tab, and they keep the PC awake. Profile changes save
automatically; a running bot keeps the settings it started with, so stop and start it to apply
changes.

### Staying polite
- **Normal** watch speed is about four requests a minute, roughly someone refreshing the page.
  Save **Fast** for short periods.
- If Resy ever says "slow down" (HTTP 429), the bots back off on their own.
- Snipe checks rapidly for only about a minute at the release.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| **Check token:** *Resy rejected this token* | It was copied while signed out, or only partly copied. Sign in, reload, copy the whole value (starts with `eyJ`). |
| Profile shows **🔑 Token expired** | Tokens eventually expire. Copy a fresh one, paste it, and start the bot again. |
| Snipe button is greyed out | All selected dates have already been released. Use **Watch** for those dates. |
| No alerts on the phone | Check the topic in ntfy matches the profile exactly, allow notifications for ntfy, and press **Send a test alert**. |
| Nothing at `localhost:8501` | Run `launch_app.pyw` again, or `streamlit run app.py` and read any error it prints. |

---

## Your data

Everything personal stays on your computer in the **`data/`** folder, which is excluded from git:
profiles (including Resy tokens), bot logs, the tracker database and your bookings history.
Back it up if you like, but never share or commit it.

## Project layout

```
app.py              Streamlit app entry point (pages are in ui/)
ui/                 Dashboard, Openings, Profiles, Logs, Help
resybot/            bots, tracker, Resy client, phone alerts, profile storage
launch_app.pyw      Windows launcher: starts the app in the background, opens the browser
data/               your private data (created on first run, git-ignored)
```

Developers: set `RESY_DATA=<folder>` to run against a separate data folder, for example to test without
touching real profiles: `RESY_DATA=./test-data streamlit run app.py --server.port 8502`.
