"""📜 Logs: every bot's and the tracker's output, color-coded and searchable."""
from __future__ import annotations

import streamlit as st

from resybot import procs, profiles
from ui.common import MODES, log_html, tail_lines


def sources() -> dict[str, str]:
    out = {}
    for s in profiles.list_profiles():
        for m in MODES:
            out[f"{profiles.label(s)} · {m}"] = procs.log_path(s, m)
    out["Cancellation tracker"] = procs.TRACKER_LOG
    return out


@st.fragment(run_every="3s")
def viewer(path: str, query: str, hide_debug: bool, n: int):
    st.markdown(log_html(tail_lines(path, n), query=query, hide_debug=hide_debug), unsafe_allow_html=True)


def render():
    st.title("Logs")
    st.caption("Newest first. Bookings and openings are highlighted green, warnings amber, errors red.")
    src = sources()
    a, b, c, d = st.columns([2, 2, 1, 1], vertical_alignment="bottom")
    pick = a.selectbox("Show", list(src))
    query = b.text_input("Search", placeholder="e.g. Opening, 19:00, TOKEN")
    n = c.selectbox("Lines", [200, 500, 2000], index=1)
    hide_debug = d.toggle("Hide request noise", value=True, help="Hides the per-request DEBUG lines.")
    viewer(src[pick], query, hide_debug, n)
    lines = tail_lines(src[pick], 100000)
    st.download_button("Download full log", "".join(lines), file_name=f"{pick.replace(' · ', '_')}.log",
                       disabled=not lines)


render()
