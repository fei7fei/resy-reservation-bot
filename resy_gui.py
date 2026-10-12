"""Simple desktop UI for resy_bot: edit config.yaml and start/stop the bot.

Usage:
    python resy_gui.py [--config config.yaml]
"""
from __future__ import annotations

import argparse
import calendar
import datetime as dt
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import sv_ttk
import yaml

from resy_client import ResyClient, booking_policy, parse_venue_url

HERE = os.path.dirname(os.path.abspath(__file__))
MODES = ("watch", "snipe")

# 15-minute increments, shown as "6:30 PM", stored as "18:30".
TIME_VALUES = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 15, 30, 45)]


def to_12h(hhmm: str) -> str:
    return dt.datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0")


def from_12h(label: str) -> str:
    return dt.datetime.strptime(label.strip().upper(), "%I:%M %p").strftime("%H:%M")


TIME_LABELS = [to_12h(t) for t in TIME_VALUES]

# Colors for the few classic-tk widgets (log, calendar days) the theme doesn't style.
PALETTE = {
    "light": {"hint": "#6b7280", "log_bg": "#f7f7f7", "log_fg": "#1f2937", "day_bg": "#fafafa", "day_fg": "#1f2937",
              "day_off": "#c4c4c4", "accent": "#005fb8", "accent_fg": "white", "hover": "#cfe4f7"},
    "dark": {"hint": "#9ca3af", "log_bg": "#1c1c1c", "log_fg": "#e5e7eb", "day_bg": "#2b2b2b", "day_fg": "#f3f4f6",
             "day_off": "#5a5a5a", "accent": "#57c8ff", "accent_fg": "black", "hover": "#3a4a5a"},
}


def system_theme() -> str:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
    except Exception:
        return "light"


class DatePicker(tk.Toplevel):
    """Month calendar where clicking a day toggles it. Closed weekdays and past days are disabled."""

    def __init__(self, parent: tk.Misc, selected: list[str], closed_weekdays: set[int], on_done, colors: dict):
        super().__init__(parent)
        self.colors = colors
        self.title("Pick dates")
        self.resizable(False, False)
        self.transient(parent)
        self.selected = {dt.date.fromisoformat(d) for d in selected}
        self.closed = closed_weekdays
        self.on_done = on_done
        first = min(self.selected) if self.selected else dt.date.today()
        self.year, self.month = first.year, first.month

        head = ttk.Frame(self, padding=(8, 8, 8, 0))
        head.pack(fill="x")
        ttk.Button(head, text="◀", width=3, command=lambda: self._shift(-1)).pack(side="left")
        self.title_var = tk.StringVar()
        ttk.Label(head, textvariable=self.title_var, font=("Segoe UI", 11, "bold"), anchor="center").pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(head, text="▶", width=3, command=lambda: self._shift(1)).pack(side="right")

        self.grid_frame = ttk.Frame(self, padding=8)
        self.grid_frame.pack()

        self.summary_var = tk.StringVar()
        ttk.Label(self, textvariable=self.summary_var, foreground=colors["hint"], wraplength=300).pack(padx=8)
        btns = ttk.Frame(self, padding=8)
        btns.pack(fill="x")
        ttk.Button(btns, text="Clear", command=self._clear).pack(side="left")
        ttk.Button(btns, text="Done", style="Accent.TButton", command=self._done).pack(side="right")

        self._render()
        self.grab_set()

    def _shift(self, delta: int):
        m = self.month - 1 + delta
        self.year, self.month = self.year + m // 12, m % 12 + 1
        self._render()

    def _toggle(self, d: dt.date):
        self.selected.symmetric_difference_update({d})
        self._render()

    def _clear(self):
        self.selected.clear()
        self._render()

    def _render(self):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        self.title_var.set(dt.date(self.year, self.month, 1).strftime("%B %Y"))
        for i, name in enumerate(["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]):
            ttk.Label(self.grid_frame, text=name,
                      foreground=self.colors["hint"] if i not in self.closed else self.colors["day_off"]).grid(
                row=0, column=i, padx=2
            )
        today = dt.date.today()
        for r, week in enumerate(calendar.Calendar().monthdatescalendar(self.year, self.month), start=1):
            for c, d in enumerate(week):
                if d.month != self.month:
                    continue
                disabled = d < today or d.weekday() in self.closed
                sel = d in self.selected
                k = self.colors
                b = tk.Button(
                    self.grid_frame, text=str(d.day), width=3, relief="flat", bd=0, pady=4,
                    font=("Segoe UI", 10, "bold" if sel else "normal"),
                    bg=k["accent"] if sel else k["day_bg"], fg=k["accent_fg"] if sel else k["day_fg"],
                    activebackground=k["hover"], activeforeground=k["day_fg"], disabledforeground=k["day_off"],
                    state="disabled" if disabled and not sel else "normal", cursor="hand2",
                    command=lambda d=d: self._toggle(d),
                )
                b.grid(row=r, column=c, padx=1, pady=1)
        picked = sorted(self.selected)
        self.summary_var.set(
            ("Selected: " + ", ".join(f"{d:%a %b} {d.day}" for d in picked)) if picked else "Click days to select them."
        )

    def _done(self):
        self.on_done([d.isoformat() for d in sorted(self.selected)])
        self.destroy()


class App(tk.Tk):
    def __init__(self, config_path: str):
        super().__init__()
        self.config_path = config_path
        self.cfg: dict = {}
        self.procs: dict[str, subprocess.Popen] = {}  # one bot per mode, can run side by side
        self.log_queue: queue.Queue = queue.Queue()
        self.ui_queue: queue.Queue = queue.Queue()  # callbacks from worker threads, run on the Tk thread

        self.title("Resy Bot")
        self.geometry(f"780x{min(990, self.winfo_screenheight() - 90)}+40+10")
        self.minsize(660, 660)

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                saved_theme = (yaml.safe_load(f) or {}).get("ui_theme")
        except (OSError, yaml.YAMLError):
            saved_theme = None
        self.theme = saved_theme if saved_theme in PALETTE else system_theme()
        sv_ttk.set_theme(self.theme)

        self._build()
        self._apply_theme(self.theme)
        self._load()
        self.after(100, self._drain_log)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- layout -----------------------------------------------------------

    def _build(self):
        pad = {"padx": 8, "pady": 4}
        root = ttk.Frame(self, padding=(14, 10))
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root)
        header.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(header, text="🍽  Resy Bot", font=("Segoe UI Variable Display", 18, "bold")).pack(side="left")
        self.dark_var = tk.BooleanVar(value=self.theme == "dark")
        ttk.Checkbutton(header, text="Dark mode", style="Switch.TCheckbutton", variable=self.dark_var,
                        command=lambda: self._apply_theme("dark" if self.dark_var.get() else "light", save=True)
                        ).pack(side="right")

        # Restaurant lookup
        rest = ttk.LabelFrame(root, text="Restaurant", padding=8)
        rest.pack(fill="x", **pad)
        self.url_var = tk.StringVar()
        ttk.Label(rest, text="Resy link").grid(row=0, column=0, sticky="w")
        url_entry = ttk.Entry(rest, textvariable=self.url_var)
        url_entry.grid(row=0, column=1, sticky="we", **pad)
        url_entry.bind("<Return>", lambda _e: self._lookup())
        self.lookup_btn = ttk.Button(rest, text="Look up", command=self._lookup)
        self.lookup_btn.grid(row=0, column=2)
        self.venue_info_var = tk.StringVar(value="Paste the restaurant's resy.com page link and click Look up.")
        ttk.Label(rest, textvariable=self.venue_info_var, wraplength=660, justify="left").grid(
            row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(4, 0)
        )
        self.policy_var = tk.StringVar()
        ttk.Label(rest, textvariable=self.policy_var, wraplength=660, justify="left", style="Hint.TLabel").grid(
            row=2, column=0, columnspan=3, sticky="w", padx=8
        )
        rest.columnconfigure(1, weight=1)

        # Reservation
        res = ttk.LabelFrame(root, text="Reservation", padding=8)
        res.pack(fill="x", **pad)

        self.venue_var = tk.StringVar()
        self.party_var = tk.StringVar()
        self.dates_var = tk.StringVar()
        self.start_var = tk.StringVar()
        self.end_var = tk.StringVar()
        self.pref_var = tk.StringVar()
        self.seating_var = tk.StringVar()

        ttk.Label(res, text="Venue ID").grid(row=0, column=0, sticky="w")
        ttk.Entry(res, textvariable=self.venue_var, width=12, state="readonly").grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(res, text="Party size").grid(row=0, column=2, sticky="w")
        self.party_spin = ttk.Spinbox(res, from_=1, to=20, textvariable=self.party_var, width=5)
        self.party_spin.grid(row=0, column=3, sticky="w", **pad)

        ttk.Label(res, text="Date(s)").grid(row=1, column=0, sticky="w")
        dates_row = ttk.Frame(res)
        dates_row.grid(row=1, column=1, columnspan=3, sticky="we", **pad)
        ttk.Entry(dates_row, textvariable=self.dates_var).pack(side="left", fill="x", expand=True)
        ttk.Button(dates_row, text="📅 Pick…", command=self._pick_dates).pack(side="left", padx=(6, 0))
        self.dates_hint_var = tk.StringVar()
        ttk.Label(res, textvariable=self.dates_hint_var, style="Hint.TLabel").grid(
            row=2, column=1, columnspan=3, sticky="w", padx=8
        )
        self.dates_var.trace_add("write", lambda *_: self._refresh_hints())

        ttk.Label(res, text="Earliest time").grid(row=3, column=0, sticky="w")
        ttk.Combobox(res, textvariable=self.start_var, values=TIME_LABELS, width=10).grid(row=3, column=1, sticky="w", **pad)
        ttk.Label(res, text="Latest time").grid(row=3, column=2, sticky="w")
        ttk.Combobox(res, textvariable=self.end_var, values=TIME_LABELS, width=10).grid(row=3, column=3, sticky="w", **pad)
        ttk.Label(res, text="Preferred time").grid(row=4, column=0, sticky="w")
        ttk.Combobox(res, textvariable=self.pref_var, values=["(none)"] + TIME_LABELS, width=10).grid(row=4, column=1, sticky="w", **pad)
        ttk.Label(res, text="Tried first; otherwise earliest → latest in the window.", style="Hint.TLabel").grid(
            row=4, column=2, columnspan=2, sticky="w", padx=8
        )
        ttk.Label(res, text="Seating type").grid(row=5, column=0, sticky="w")
        ttk.Entry(res, textvariable=self.seating_var, width=24).grid(row=5, column=1, columnspan=2, sticky="w", **pad)
        ttk.Label(res, text="Optional, e.g. Dining room (comma-separated). Blank = any.", style="Hint.TLabel").grid(
            row=6, column=1, columnspan=3, sticky="w", padx=8
        )
        res.columnconfigure(3, weight=1)

        # Mode
        mode = ttk.LabelFrame(root, text="Mode", padding=8)
        mode.pack(fill="x", **pad)

        self.mode_var = tk.StringVar(value="watch")
        ttk.Radiobutton(mode, text="Watch (catch cancellations)", variable=self.mode_var, value="watch",
                        command=self._sync_mode).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(mode, text="Snipe (booking window opens at a set time)", variable=self.mode_var, value="snipe",
                        command=self._sync_mode).grid(row=0, column=2, columnspan=2, sticky="w")

        self.watch_frame = ttk.Frame(mode)
        self.watch_frame.grid(row=1, column=0, columnspan=4, sticky="we", pady=(6, 0))
        self.poll_var = tk.StringVar()
        self.stop_at_var = tk.StringVar()
        ttk.Label(self.watch_frame, text="Check every (sec)").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(self.watch_frame, from_=10, to=600, textvariable=self.poll_var, width=6).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(self.watch_frame, text="Stop at (optional)").grid(row=0, column=2, sticky="w")
        ttk.Entry(self.watch_frame, textvariable=self.stop_at_var, width=20).grid(row=0, column=3, sticky="w", **pad)
        ttk.Label(self.watch_frame, text="YYYY-MM-DD HH:MM", style="Hint.TLabel").grid(row=1, column=3, sticky="w", padx=8)

        self.snipe_frame = ttk.Frame(mode)
        self.snipe_frame.grid(row=1, column=0, columnspan=4, sticky="we", pady=(6, 0))
        self.days_ahead_var = tk.StringVar(value="30")
        self.release_var = tk.StringVar(value="09:00:00")
        ttk.Label(self.snipe_frame, text="Opens").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(self.snipe_frame, from_=1, to=365, textvariable=self.days_ahead_var, width=5).grid(
            row=0, column=1, sticky="w", **pad
        )
        ttk.Label(self.snipe_frame, text="days before each date, at").grid(row=0, column=2, sticky="w")
        ttk.Entry(self.snipe_frame, textvariable=self.release_var, width=10).grid(row=0, column=3, sticky="w", **pad)
        ttk.Label(self.snipe_frame, text="HH:MM:SS, 24h, your local time", style="Hint.TLabel").grid(
            row=0, column=4, sticky="w"
        )
        self.plan_var = tk.StringVar()
        ttk.Label(self.snipe_frame, textvariable=self.plan_var, justify="left").grid(
            row=1, column=0, columnspan=5, sticky="w", pady=(4, 0)
        )
        self.days_ahead_var.trace_add("write", lambda *_: self._refresh_hints())
        self.release_var.trace_add("write", lambda *_: self._refresh_hints())

        # Account (masked)
        acct = ttk.LabelFrame(root, text="Account", padding=8)
        acct.pack(fill="x", **pad)
        self.api_key_var = tk.StringVar()
        self.token_var = tk.StringVar()
        ttk.Label(acct, text="API key (optional)").grid(row=0, column=0, sticky="w")
        self.api_entry = ttk.Entry(acct, textvariable=self.api_key_var, show="•", width=50)
        self.api_entry.grid(row=0, column=1, sticky="we", **pad)
        ttk.Label(acct, text="Auth token").grid(row=1, column=0, sticky="w")
        self.token_entry = ttk.Entry(acct, textvariable=self.token_var, show="•", width=50)
        self.token_entry.grid(row=1, column=1, sticky="we", **pad)
        self.show_var = tk.BooleanVar()
        ttk.Checkbutton(acct, text="Show", variable=self.show_var, command=self._toggle_show).grid(row=0, column=2, rowspan=2)
        acct.columnconfigure(1, weight=1)

        # Controls
        ctl = ttk.Frame(root)
        ctl.pack(fill="x", **pad)
        opts = ttk.Frame(ctl)
        opts.pack(side="left")
        self.dry_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Dry run (test only, don't actually book)", variable=self.dry_var).pack(anchor="w")
        self.stop_first_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Stop after the first booking", variable=self.stop_first_var).pack(anchor="w")
        self.stop_btns: dict[str, ttk.Button] = {}
        for m in reversed(MODES):
            b = ttk.Button(ctl, text=f"Stop {m}", command=lambda m=m: self._stop(m), state="disabled")
            b.pack(side="right", padx=4)
            self.stop_btns[m] = b
        self.start_btn = ttk.Button(ctl, text="Save & Start", style="Accent.TButton", command=self._start)
        self.start_btn.pack(side="right", padx=4)
        ttk.Button(ctl, text="Save", command=self._save).pack(side="right", padx=4)

        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(root, textvariable=self.status_var, style="Hint.TLabel").pack(anchor="w", padx=8)

        # Log
        logf = ttk.LabelFrame(root, text="Log", padding=4)
        logf.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(logf, height=7, wrap="word", state="disabled", font=("Cascadia Mono", 9),
                           relief="flat", bd=0, padx=8, pady=6, highlightthickness=0)
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

    def _apply_theme(self, theme: str, save: bool = False):
        self.theme = theme
        sv_ttk.set_theme(theme)
        k = PALETTE[theme]
        # The theme ignores foreground on derived label styles, so color hint labels directly.
        stack = [self]
        while stack:
            w = stack.pop()
            stack.extend(w.winfo_children())
            if isinstance(w, ttk.Label) and str(w.cget("style")) == "Hint.TLabel":
                w.configure(foreground=k["hint"])
        self.log.configure(bg=k["log_bg"], fg=k["log_fg"], insertbackground=k["log_fg"])
        if save:  # remember the choice without touching the rest of config.yaml
            self.cfg["ui_theme"] = theme
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    disk = yaml.safe_load(f) or {}
                disk["ui_theme"] = theme
                self._write_config(self.config_path, disk)
            except (OSError, yaml.YAMLError):
                pass

    def _sync_mode(self):
        if self.mode_var.get() == "watch":
            self.snipe_frame.grid_remove()
            self.watch_frame.grid()
        else:
            self.watch_frame.grid_remove()
            self.snipe_frame.grid()
        self._refresh_buttons()

    def _toggle_show(self):
        ch = "" if self.show_var.get() else "•"
        self.api_entry.configure(show=ch)
        self.token_entry.configure(show=ch)

    def _pick_dates(self):
        try:
            current = self._dates()
        except ValueError:
            current = []
        DatePicker(self, current, set(self.cfg.get("closed_weekdays") or []),
                   lambda picked: self.dates_var.set(", ".join(picked)), PALETTE[self.theme])

    def _snipe_plan(self) -> list[tuple[dt.datetime, str]]:
        """(release moment, date) for each selected date. Raises ValueError on bad input."""
        n = int(self.days_ahead_var.get())
        rel = dt.datetime.strptime(self._release_time(), "%H:%M:%S").time()
        return sorted((dt.datetime.combine(dt.date.fromisoformat(d) - dt.timedelta(days=n), rel), d) for d in self._dates())

    def _release_time(self) -> str:
        t = self.release_var.get().strip() or "09:00:00"
        return t + ":00" if len(t) == 5 else t

    def _refresh_hints(self):
        try:
            dates = self._dates()
        except ValueError:
            self.dates_hint_var.set("Dates must look like 2026-11-10, comma-separated — or use Pick….")
            return
        if dates:
            self.dates_hint_var.set(
                f"{len(dates)} date(s): " + ", ".join(f"{dt.date.fromisoformat(d):%a %b} {int(d[8:])}" for d in dates)
            )
        else:
            self.dates_hint_var.set("Use Pick… to choose one or more days on a calendar.")
        try:
            plan = self._snipe_plan()
        except ValueError:
            self.plan_var.set("")
            return
        now = dt.datetime.now()
        lines = []
        for when, d in plan:
            tag = "  (already opened — use Watch)" if when < now else ""
            lines.append(f"• {dt.date.fromisoformat(d):%a %b} {int(d[8:])}  →  opens {when:%a %b} {when.day}, "
                         f"{when:%I:%M %p}".replace(" 0", " ") + tag)
        self.plan_var.set("\n".join(lines))

    def _lookup(self):
        try:
            location, slug = parse_venue_url(self.url_var.get())
        except ValueError as e:
            messagebox.showerror("Resy Bot", str(e))
            return
        self.lookup_btn.configure(state="disabled")
        self.venue_info_var.set("Looking up...")
        self.policy_var.set("")
        try:
            party = int(self.party_var.get())
        except ValueError:
            party = 2
        api_key = self.api_key_var.get().strip() or None

        def work():
            try:
                client = ResyClient(api_key=api_key)
                venue = client.get_venue(slug, location)
                venue_id = venue["id"]["resy"]
                cal = client.get_calendar(venue_id, party)
                self.ui_queue.put(lambda: self._lookup_done(venue, cal, None))
            except Exception as e:  # network / API errors -> show in UI
                self.ui_queue.put(lambda e=e: self._lookup_done(None, None, e))

        threading.Thread(target=work, daemon=True).start()

    def _lookup_done(self, venue: dict | None, cal: dict | None, err: Exception | None):
        self.lookup_btn.configure(state="normal")
        if err:
            self.venue_info_var.set(f"Lookup failed: {err}")
            return

        venue_id = venue["id"]["resy"]
        name = venue.get("name", "?")
        city = (venue.get("location") or {}).get("locality", "")
        self.venue_var.set(str(venue_id))
        self.cfg["restaurant_name"] = name
        self.cfg["restaurant_url"] = self.url_var.get().strip()

        lo, hi = venue.get("min_party_size") or 1, venue.get("max_party_size") or 20
        self.party_spin.configure(from_=lo, to=hi)

        policy = booking_policy(venue)
        if policy.get("release_time"):
            self.release_var.set(policy["release_time"])
        last_day = cal.get("last_calendar_day")
        # The calendar is ground truth (venue text can be off by one, e.g. says
        # 30 days when it's really 29). Prefer it; fall back to the text.
        days_ahead = None
        if last_day:
            days_ahead = (dt.date.fromisoformat(last_day) - dt.date.today()).days
            # Before today's release time, today's new date hasn't unlocked yet.
            try:
                if dt.datetime.now().time() < dt.datetime.strptime(self._release_time(), "%H:%M:%S").time():
                    days_ahead += 1
            except ValueError:
                pass
        if not days_ahead:
            days_ahead = policy.get("days_ahead")
        if days_ahead:
            self.days_ahead_var.set(str(days_ahead))

        statuses: dict[int, set] = {}
        for d in cal.get("scheduled", []):
            statuses.setdefault(dt.date.fromisoformat(d["date"]).weekday(), set()).add((d.get("inventory") or {}).get("reservation"))
        closed = sorted(wd for wd, st in statuses.items() if st == {"closed"})
        self.cfg["closed_weekdays"] = closed
        open_days = [d["date"] for d in cal.get("scheduled", [])
                     if (d.get("inventory") or {}).get("reservation") == "available"]
        parts = [f"{name} — {city}  (venue {venue_id})", f"party {lo}–{hi}"]
        if days_ahead:
            parts.append(f"books ~{days_ahead} days ahead")
        if last_day:
            parts.append(f"bookable through {last_day}")
        if closed:
            parts.append("closed " + ", ".join(dt.date(2024, 1, 1 + wd).strftime("%A") + "s" for wd in closed))
        info = " · ".join(parts)
        if open_days:
            shown = ", ".join(d[5:] for d in open_days[:8]) + (" ..." if len(open_days) > 8 else "")
            info += f"\nDates with open tables now (party of {self.party_var.get()}): {shown}"
        else:
            info += f"\nNo open tables right now for a party of {self.party_var.get()}."
        if not policy.get("release_time"):
            info += "\nRelease time of day not listed — check the venue page if you plan to snipe."
        self.venue_info_var.set(info)
        text = policy.get("text", "")
        self.policy_var.set(("Need to know: " + text[:400] + ("..." if len(text) > 400 else "")) if text else "")
        self._refresh_hints()
        self.status_var.set(f"Loaded {name}. Review the fields, then Save.")

    # -- config load/save -------------------------------------------------

    def _load(self):
        if os.path.exists(self.config_path):
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.cfg = yaml.safe_load(f) or {}
        c = self.cfg
        self.venue_var.set(str(c.get("venue_id", "")))
        self.url_var.set(c.get("restaurant_url", "") or "")
        if c.get("restaurant_name"):
            self.venue_info_var.set(f"{c['restaurant_name']}  (venue {c.get('venue_id')}) -- click Look up to refresh.")
        self.party_var.set(str(c.get("party_size", 2)))
        dates = c.get("watch_dates") or ([c["reservation_date"]] if c.get("reservation_date") else [])
        if c.get("reservation_date") and c["reservation_date"] not in dates:
            dates = [c["reservation_date"]] + list(dates)
        self.start_var.set(to_12h(c.get("preferred_time_start", "18:00")))
        self.end_var.set(to_12h(c.get("preferred_time_end", "21:00")))
        self.pref_var.set(to_12h(c["preferred_time"]) if c.get("preferred_time") else "(none)")
        self.seating_var.set(", ".join(c.get("seating_types") or []))
        self.poll_var.set(str(c.get("watch_poll_interval_seconds", 20)))
        stop_at = c.get("watch_stop_at") or ""
        self.stop_at_var.set(str(stop_at)[:16])

        # Snipe: prefer the per-date settings; derive them from an old single drop_at if that's all we have.
        release = c.get("release_time")
        days_ahead = c.get("snipe_days_ahead")
        if (release is None or days_ahead is None) and c.get("drop_at") and c.get("reservation_date"):
            drop = dt.datetime.strptime(str(c["drop_at"]), "%Y-%m-%d %H:%M:%S")
            release = release or drop.strftime("%H:%M:%S")
            days_ahead = days_ahead or (dt.date.fromisoformat(c["reservation_date"]) - drop.date()).days
        self.release_var.set(release or "09:00:00")
        self.days_ahead_var.set(str(days_ahead or 30))

        self.api_key_var.set(c.get("api_key", "") or "")
        self.token_var.set(c.get("auth_token", "") or "")
        self.dry_var.set(bool(c.get("dry_run", True)))
        self.stop_first_var.set(bool(c.get("stop_after_first_booking", True)))
        self.dates_var.set(", ".join(str(d) for d in dates))  # last: triggers hint refresh
        self._sync_mode()

    def _dates(self) -> list[str]:
        out = []
        for part in self.dates_var.get().split(","):
            part = part.strip()
            if part:
                dt.date.fromisoformat(part)  # validates
                out.append(part)
        return sorted(set(out))

    def _collect(self) -> dict:
        """Validate the form and return an updated config dict. Raises ValueError with a friendly message."""
        c = dict(self.cfg)
        try:
            c["venue_id"] = int(self.venue_var.get())
        except ValueError:
            raise ValueError("Paste the restaurant's Resy link and click Look up first.")
        try:
            c["party_size"] = int(self.party_var.get())
        except ValueError:
            raise ValueError("Party size must be a number.")
        try:
            dates = self._dates()
        except ValueError:
            raise ValueError("Dates must look like 2026-11-10 (comma-separated for several) — or use Pick….")
        if not dates:
            raise ValueError("Pick at least one date.")
        c["reservation_date"] = dates[0]
        c["watch_dates"] = dates
        try:
            c["preferred_time_start"] = from_12h(self.start_var.get())
            c["preferred_time_end"] = from_12h(self.end_var.get())
            pref = self.pref_var.get().strip()
            if pref and pref != "(none)":
                c["preferred_time"] = from_12h(pref)
            else:
                c.pop("preferred_time", None)
        except ValueError:
            raise ValueError("Times must look like 6:30 PM.")
        seating = [x.strip() for x in self.seating_var.get().split(",") if x.strip()]
        if seating:
            c["seating_types"] = seating
        else:
            c.pop("seating_types", None)
        pref = c.get("preferred_time")
        if pref:
            lo, hi = c["preferred_time_start"], c["preferred_time_end"]
            inside = lo <= pref <= hi if lo <= hi else (pref >= lo or pref <= hi)
            if not inside:
                raise ValueError("Preferred time must be between the earliest and latest times.")

        try:
            c["watch_poll_interval_seconds"] = max(10, int(float(self.poll_var.get())))
        except ValueError:
            raise ValueError("'Check every' must be a number of seconds.")
        stop_at = self.stop_at_var.get().strip()
        if stop_at:
            try:
                c["watch_stop_at"] = dt.datetime.strptime(stop_at, "%Y-%m-%d %H:%M").strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                raise ValueError("'Stop at' must look like 2026-11-09 17:00.")
        else:
            c.pop("watch_stop_at", None)

        try:
            plan = self._snipe_plan()
        except ValueError:
            raise ValueError("Snipe needs a number of days and a time like 09:00:00.")
        c["snipe_days_ahead"] = int(self.days_ahead_var.get())
        c["release_time"] = self._release_time()
        c["drop_at"] = plan[0][0].strftime("%Y-%m-%d %H:%M:%S")  # first release, for older bot versions

        api_key = self.api_key_var.get().strip()
        if api_key and "PASTE" not in api_key:
            c["api_key"] = api_key
        else:
            c.pop("api_key", None)  # client falls back to resy.com's public key
        token = self.token_var.get().strip() or self._token_on_disk()
        if token:
            c["auth_token"] = token
            self.token_var.set(token)
        else:
            c.pop("auth_token", None)
        c["dry_run"] = bool(self.dry_var.get())
        c["stop_after_first_booking"] = bool(self.stop_first_var.get())
        return c

    def _token_on_disk(self) -> str:
        """Keep a token pasted straight into config.yaml while the UI was open."""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                t = (yaml.safe_load(f) or {}).get("auth_token") or ""
        except (OSError, yaml.YAMLError):
            return ""
        return "" if "PASTE" in t else t.strip()

    def _write_config(self, path: str, c: dict):
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Written by resy_gui.py. Contains credentials -- don't share or commit.\n")
            yaml.safe_dump(c, f, sort_keys=False, allow_unicode=True)

    def _save(self) -> bool:
        try:
            c = self._collect()
        except ValueError as e:
            messagebox.showerror("Resy Bot", str(e))
            return False
        closed = set(c.get("closed_weekdays") or [])
        bad = [d for d in c["watch_dates"] if dt.date.fromisoformat(d).weekday() in closed]
        if bad and not messagebox.askyesno(
            "Resy Bot",
            "The restaurant looks closed on: "
            + ", ".join(f"{d} ({dt.date.fromisoformat(d):%A})" for d in bad)
            + "\n\nSave anyway?",
        ):
            return False
        self._write_config(self.config_path, c)
        self.cfg = c
        self.status_var.set(f"Saved {os.path.basename(self.config_path)}")
        return True

    # -- running the bot --------------------------------------------------

    def _running(self, mode: str) -> bool:
        p = self.procs.get(mode)
        return bool(p and p.poll() is None)

    def _refresh_buttons(self):
        for m, b in self.stop_btns.items():
            b.configure(state="normal" if self._running(m) else "disabled")
        mode = self.mode_var.get()
        self.start_btn.configure(state="disabled" if self._running(mode) else "normal",
                                 text=f"Save & Start {mode}")
        running = [m for m in MODES if self._running(m)]
        self.status_var.set(("Running: " + ", ".join(running)) if running else "Idle")

    def _start(self):
        mode = self.mode_var.get()
        if self._running(mode):
            return
        if not self._save():
            return
        if mode == "snipe":
            now = dt.datetime.now()
            if all(when < now - dt.timedelta(seconds=self.cfg.get("burst_timeout_seconds", 90))
                   for when, _ in self._snipe_plan()):
                messagebox.showerror("Resy Bot", "Every selected date has already opened. Use Watch to catch cancellations.")
                return
        if not self.cfg.get("dry_run") and not messagebox.askyesno(
            "Resy Bot", f"Dry run is OFF -- the {mode} bot will make a real booking on your account. Continue?"
        ):
            return
        # Each bot gets its own snapshot of the settings, so editing the form
        # (or starting the other mode) doesn't change a bot that's already running.
        run_cfg = os.path.join(HERE, f".run_{mode}.yaml")
        self._write_config(run_cfg, self.cfg)
        cmd = [sys.executable, "-u", os.path.join(HERE, "resy_bot.py"), mode, "--config", run_cfg, "-v"]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.Popen(
            cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags,
        )
        self.procs[mode] = proc
        threading.Thread(target=self._pump, args=(mode, proc), daemon=True).start()
        dates = ", ".join(self.cfg["watch_dates"])
        self._append(f"--- [{mode}] started ({'dry run' if self.cfg.get('dry_run') else 'LIVE'}) for {dates} ---\n")
        self._refresh_buttons()

    def _pump(self, mode: str, proc: subprocess.Popen):
        for line in proc.stdout:
            self.log_queue.put(f"[{mode}] {line}")
        proc.wait()
        self.log_queue.put(f"--- [{mode}] bot exited (code {proc.returncode}) ---\n")
        self.log_queue.put(None)  # sentinel: a process ended

    def _stop(self, mode: str):
        if self._running(mode):
            self.procs[mode].terminate()

    def _drain_log(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                if line is None:
                    self._refresh_buttons()
                else:
                    self._append(line)
        except queue.Empty:
            pass
        while not self.ui_queue.empty():
            self.ui_queue.get_nowait()()
        self.after(100, self._drain_log)

    def _append(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _on_close(self):
        running = [m for m in MODES if self._running(m)]
        if running:
            if not messagebox.askyesno("Resy Bot", f"Still running: {', '.join(running)}. Stop and quit?"):
                return
            for m in running:
                self.procs[m].terminate()
        self.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    args = parser.parse_args()
    App(os.path.abspath(args.config)).mainloop()


if __name__ == "__main__":
    main()
