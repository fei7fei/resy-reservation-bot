"""❓ Help: connecting a Resy account, phone alerts, and how the bots behave."""
import streamlit as st

st.title("Help")

# -- connecting an account -----------------------------------------------------------
with st.container(border=True):
    st.subheader("🔗 Connect a Resy account")
    st.markdown(
        "The bot books on a Resy account using that account's **login token** — the same key your browser "
        "uses once you're signed in to resy.com. You copy it from the browser once; no password is stored. "
        "It takes about two minutes, on a **computer** (phones can't show the token)."
    )

    one, two, three = st.columns(3, gap="large")
    with one:
        st.markdown("""
**1 · Set up the profile**

1. Open **👥 Profiles**.
2. Your account: open **Me**.
   A friend: type their name under **Add a profile** → **Create**.
3. Paste the restaurant's **resy.com link** and press **Look up**.
4. Pick **dates** and a **time window**.
""")
    with two:
        st.markdown("""
**2 · Copy the token**

1. On a computer, sign in at **resy.com** with the account that should get the booking.
2. Follow the steps for your browser below.
3. The token is one long line starting with **`eyJ`** — copy all of it.
""")
    with three:
        st.markdown("""
**3 · Paste and check**

1. In the profile's **🔑 Account** box, paste into **Resy auth token**.
2. Press **Check token**.
3. ✅ *Signed in as …* = connected. It also shows how many cards are on file.
4. Do a **dry run** (Dry run on → ▶ Start watch) to see it working before going live.
""")

    st.markdown("**Finding the token in your browser**")
    chrome, safari, firefox = st.tabs(["Chrome / Edge / Brave", "Safari (Mac)", "Firefox"])
    with chrome:
        st.markdown("""
1. On **resy.com** (signed in), press **F12** — or right-click the page → **Inspect**.
2. Click the **Network** tab at the top of the panel, then press **F5** to reload the page.
3. In the panel's **Filter** box type `api.resy.com`.
4. Click any request in the list — **user** or **find** work well.
5. On the right, open **Headers** and scroll down to **Request Headers**.
6. Find **`X-Resy-Auth-Token`** → right-click its value → **Copy value**.
""")
    with safari:
        st.markdown("""
1. Turn on developer tools once: **Safari → Settings → Advanced →** tick **Show features for web developers**
   (older Macs: *Show Develop menu in menu bar*).
2. On **resy.com** (signed in), press **⌥ Option + ⌘ Cmd + I**.
3. Open the **Network** tab and reload the page (**⌘ R**).
4. Filter for `api.resy.com` and click a request such as **user**.
5. Under **Request** headers, find **`X-Resy-Auth-Token`** and copy its value.
""")
    with firefox:
        st.markdown("""
1. On **resy.com** (signed in), press **F12** and open the **Network** tab.
2. Press **F5** to reload, then type `api.resy.com` in **Filter URLs**.
3. Click a request such as **user** → **Headers** → **Request Headers**.
4. Find **`X-Resy-Auth-Token`**, right-click it → **Copy Value**.
""")

    with st.expander("Connecting a friend's account"):
        st.markdown("""
- The friend signs in to **their own** resy.com and copies **their** token (steps above) — on their computer
  or yours. Bookings, cards and cancellation fees are on **their** account.
- Send the token privately (in person or a private message you then delete) — it works like a password.
- Give them their own **📱 Phone alerts** topic so they get their own booking alerts.
- Their profile's bots make their own requests, so they don't add to your account's traffic.
""")

    with st.expander("Before going live: a card on file"):
        st.markdown("""
Many sought-after restaurants require a card to hold the table. The bot uses the **first card saved on that
Resy account**. Add one in the Resy app (**Profile → Payment methods**) or on resy.com before turning
**Dry run** off. **Check token** shows how many cards are on file.
""")

    with st.expander("Problems?"):
        st.markdown("""
| What you see | What to do |
|---|---|
| **Check token:** *Resy rejected this token* | It was copied while signed out, or not all of it was copied. Sign in, reload, copy the whole value (starts with `eyJ`). |
| No `X-Resy-Auth-Token` in the headers | You're not signed in, or you clicked a request that isn't from `api.resy.com`. Sign in, reload, and pick **user**. |
| The list in the Network tab is empty | Keep the panel open and reload the page (F5 / ⌘ R) — it only records while open. |
| **🔑 Token expired** on a profile | Tokens last a long time but do expire. Copy a fresh one, paste it, then start the bot again. |
| Signing out of resy.com | Signing out can end that token — copy a fresh one afterwards. |
""")
    st.caption("🔒 Treat a token like a password: anyone holding it can book on that account. "
               "Tokens are stored only on this PC, in the data folder.")

# -- everything else -------------------------------------------------------------------
a, b = st.columns(2, gap="large")
with a:
    with st.container(border=True):
        st.subheader("📱 Phone alerts")
        st.markdown("""
Alerts use **ntfy** — a free app, no account needed.

1. Install **ntfy** from the App Store or Google Play (green bell icon, by Philipp Heckel).
2. In a profile, switch on **Send alerts to a phone** — it shows a topic name like `resybot-3f9c2a71b0de`.
3. In the ntfy app tap **+** (*Subscribe to topic*), type that topic name exactly, keep the server
   as **ntfy.sh**, and tap **Subscribe**. (The app has no QR scanner — the QR code in the profile is
   for your phone's camera, to open the topic page so you can copy the name.)
4. Press **Send a test alert**, then **Preview a booking alert**.

Choose what to get: 🎉 **bookings** (sent as urgent), 👀 tables that opened but got taken first,
and ⚠️ problems (token expired, bot stopped, snipe missed). Each profile has its own topic, so a
friend only gets their own alerts.
""")
    with st.container(border=True):
        st.subheader("💡 Good to know")
        st.markdown("""
- Bots keep running when you close this tab, and keep the PC awake. Closing a laptop lid can still sleep it.
- Profile changes save automatically, but a bot that's already running keeps the settings it started
  with — stop and start it to apply changes. (Alert settings are the exception: they apply right away.)
- Real bookings use the **first card** saved on that Resy account, so cancellation fees go to that account.
""")

with b:
    with st.container(border=True):
        st.subheader("🤖 How the bots work")
        st.markdown("""
**👀 Watch** catches cancellations on the dates you pick. Each check is **one calendar
request** covering all your dates; a date's times are only fetched when it shows a table,
then it books the first time in your window (preferred time first).

**🎯 Snipe** waits for the daily release (e.g. 9:00 AM, 29 days out) and checks rapidly
for about a minute. It re-checks your token 5 minutes before, so an expired token
is caught in time.

**📈 Tracker** (Openings page) records every opening at your restaurants — even dates
nobody is watching — and times how fast tables get booked.

**Dry run** does everything except the final booking. Use it to test.
""")
    with st.container(border=True):
        st.subheader("🛡️ Staying under the radar")
        st.markdown("""
- **Normal** watch speed is about **4 requests a minute** — similar to someone refreshing the page.
  Use **Fast** for a few hours around when cancellations happen, not all week.
- If Resy ever answers "slow down" (429), the bots automatically back off.
- Snipe is aggressive only for ~1 minute at the release.
- Every profile uses its own account, so friends' bots don't add to your account's traffic.
""")
