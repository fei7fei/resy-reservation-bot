"""❓ Help: getting a token, phone alerts, and how the bots behave."""
import streamlit as st

st.title("Help")

a, b = st.columns(2, gap="large")
with a:
    with st.container(border=True):
        st.subheader("🔑 Getting your Resy token")
        st.markdown("""
1. In Chrome, go to **resy.com** and make sure you're **logged in**.
2. Press **F12** → open the **Network** tab → press **F5** to reload.
3. Type `api.resy.com` in the filter box and click any request (e.g. **user**).
4. **Headers** → scroll to **Request Headers** → find **`X-Resy-Auth-Token`**.
5. Right-click its value → **Copy value** (a long string starting with `eyJ`).
6. Paste it in the profile's **Resy auth token** box and press **Check token**.

Tokens last a long time but eventually expire. When one does, the bot stops,
the profile shows **🔑 Token expired**, and (if alerts are on) your phone gets a message.
Just repeat these steps.

**Treat the token like a password** — it lets anyone holding it book on that account.
""")
    with st.container(border=True):
        st.subheader("📱 Phone alerts")
        st.markdown("""
Alerts use **ntfy** — a free app, no account needed.

1. Install **ntfy** from the App Store or Google Play (green bell icon, by Philipp Heckel).
2. In a profile, switch on **Send alerts to a phone** — it shows a topic name like `resybot-3f9c2a71b0de`.
3. In the ntfy app tap **+** (*Subscribe to topic*), type that topic name exactly, keep the server
   as **ntfy.sh**, and tap **Subscribe**. (The app has no QR scanner — the QR code in the profile is
   for your phone's camera, to open the topic page so you can copy the name.)
4. Press **Send a test alert**.

You'll get: 🎉 **bookings** (always), 👀 tables that opened but got taken first, and ⚠️ problems
(token expired, bot stopped, snipe missed). Each profile has its own topic, so a friend
only gets their own alerts.
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
    with st.container(border=True):
        st.subheader("💡 Good to know")
        st.markdown("""
- Bots keep running when you close this tab, and keep the PC awake. Closing a laptop lid can still sleep it.
- Editing a profile doesn't change a bot that's already running — stop and start it to apply changes.
- Real bookings use the **first card** saved on that Resy account, so cancellation fees go to that account.
""")
