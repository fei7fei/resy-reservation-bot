# Resy auto-booker

Two modes against Resy's (undocumented) web API:

- **`snipe`** -- you know the exact moment a booking window unlocks (common
  for Michelin/hard-to-get spots: "opens every day at 10:00 AM for a table
  30 days out"). The bot sleeps until just before that moment, then hammers
  the availability endpoint and books the instant your preferred time
  appears.
- **`watch`** -- continuous polling of a date for cancellations. You give a
  rough time window (e.g. 6-9pm) and it books the first slot that opens up
  in that range, whenever that happens to be.

## Setup

```
cd resy
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Edit `config.yaml`:

1. **api_key** -- the public API key resy.com's own frontend sends. Log in
   to resy.com, open DevTools > Network, reload, click any request to
   `api.resy.com`, copy the value after `Api-Key` in the `Authorization`
   header.
2. **email/password** or **auth_token** -- either let the bot log in itself,
   or paste a token straight from a logged-in browser session
   (`X-Resy-Auth-Token` request header). The token route avoids occasionally
   hitting a captcha on the login endpoint.
3. **venue_id** -- open the restaurant's resy.com page, DevTools > Network,
   reload, and look for `venue_id=` in requests like `config?venue_id=...` or
   `calendar?venue_id=...` (or in the Payload tab of the `find` POST).
4. **payment_method_id** -- only needed for venues that require a card on
   file to hold the booking. Leave it out and the bot will auto-pick your
   first saved payment method when a venue needs one (and log which id it
   used, so you can pin it next time).

Leave `dry_run: true` the first time you run either mode -- it runs the
entire flow (search -> get book_token) and logs exactly what it *would*
book, without calling the real booking endpoint. Confirm the logs show the
restaurant/date/time you expect, then flip to `dry_run: false`.

## UI

Double-click the **Resy Bot** desktop shortcut (or `launch_app.pyw`). It starts
a local Streamlit server (localhost only) and opens `http://localhost:8501`.

Paste a restaurant's resy.com link and click **Look up** -- it fills in the
venue id, party-size limits, how many days ahead bookings open, and closed
days. Pick dates and a time window, paste your auth token in the sidebar, then
start **Watch** and/or **Snipe**. Bots run as background processes (logs in
`.runs/`), so they keep going if you close the browser tab.

The older desktop window is still available: `python resy_gui.py`.

## Running

```
python resy_bot.py snipe --config config.yaml
python resy_bot.py watch --config config.yaml
```

Both log to stdout and to `resy_bot.log`, and beep 3x on a successful
booking (or dry-run match).

## Notes / limits

- **This uses Resy's private frontend API, not an official one.** It can
  change without notice, and automated access likely isn't something Resy's
  ToS blesses. You're doing this against your own account for your own
  dining reservations -- not reselling, not scraping other users' data --
  but know the account carries the risk if Resy decides to rate-limit or
  suspend bot-like traffic. Keep `watch_poll_interval_seconds` reasonable
  (don't go below ~10-15s for long-running watch sessions); the aggressive
  `burst_poll_interval_seconds` in snipe mode is only meant to run for the
  ~minute right at a drop, not for hours.
- Snipe mode's timing precision is limited by your machine's clock and
  network latency to Resy's servers -- for genuinely contested drops (a few
  seats, hundreds of people trying at once), there's no guarantee of
  winning even with perfect timing.
- `find_slots()` returning `[]` is the normal case while polling -- it just
  means nothing's open yet, not an error.
- If `book()` or `get_book_token()` raises `NoAvailabilityError`, someone
  else grabbed the slot first; both loops just keep polling.
