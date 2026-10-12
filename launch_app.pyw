"""Double-click launcher: starts the Streamlit app in the background (if it
isn't already running) and opens it in your browser."""
import os
import socket
import subprocess
import sys
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8501
URL = f"http://localhost:{PORT}"


def is_up() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            return True
    except OSError:
        return False


if not is_up():
    exe = os.path.join(os.path.dirname(sys.executable), "python.exe")
    log = open(os.path.join(HERE, "streamlit_server.log"), "w", encoding="utf-8")
    subprocess.Popen(
        [exe, "-m", "streamlit", "run", os.path.join(HERE, "app.py")],
        cwd=HERE, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    for _ in range(60):
        if is_up():
            break
        time.sleep(0.5)

webbrowser.open(URL)
