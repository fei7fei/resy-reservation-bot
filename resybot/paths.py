"""Where everything lives on disk.

    data/profiles/<name>.yaml   one file per profile (Resy account + settings)
    data/runs/<name>/           each profile's bot snapshots, logs, run info
    data/runs/_tracker/         the cancellation tracker's log and run info
    data/openings.db            tracker database

Set RESY_DATA to use a different data folder (handy for testing).
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.abspath(os.environ.get("RESY_DATA") or os.path.join(ROOT, "data"))
PROFILES = os.path.join(DATA, "profiles")
RUNS = os.path.join(DATA, "runs")
TRACKER_RUN = os.path.join(RUNS, "_tracker")
DB = os.path.join(DATA, "openings.db")


def profile_file(slug: str) -> str:
    return os.path.join(PROFILES, f"{slug}.yaml")


def run_dir(slug: str) -> str:
    return os.path.join(RUNS, slug)
