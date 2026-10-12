"""Resy Bot -- Streamlit app entry point.

Run:  streamlit run app.py      (or double-click the "Resy Bot" desktop shortcut)

Pages live in ui/; the bots, tracker and shared logic live in resybot/.
"""
import streamlit as st

from resybot import profiles
from ui.common import inject_css, status_bar

st.set_page_config(page_title="Resy Bot", page_icon="🍽️", layout="wide")
profiles.ensure_main()
inject_css()

nav = st.navigation(
    [
        st.Page("ui/dashboard.py", title="Dashboard", icon="📊", default=True),
        st.Page("ui/openings.py", title="Openings", icon="📈"),
        st.Page("ui/profiles.py", title="Profiles", icon="👥"),
        st.Page("ui/logs.py", title="Logs", icon="📜"),
        st.Page("ui/help.py", title="Help", icon="❓"),
    ],
    position="top",
)

st.fragment(status_bar, run_every="3s")()
nav.run()
