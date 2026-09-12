#!/usr/bin/env python3
"""run_ui.py — Launcher for the AP Automation & ERP Review Visualizer.

Usage:
    python run_ui.py [--port 5000] [--no-browser]
"""
import argparse
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_FRONTEND_DIR = _ROOT / "candidate_kit" / "frontend"
if not _FRONTEND_DIR.is_dir():
    _FRONTEND_DIR = _ROOT / "frontend"

for p in [str(_ROOT), str(_ROOT / "candidate_kit"), str(_FRONTEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from candidate_kit.frontend.app import run_server


def find_free_port(preferred: int = 5000) -> int:
    for port in [preferred, 5050, 8000, 8080, 8501]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


def open_browser(url: str, delay: float = 1.0):
    time.sleep(delay)
    try:
        webbrowser.open_new_tab(url)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="Launch AP Review Visualizer")
    parser.add_argument("--port", type=int, default=0, help="Port to listen on (default auto-selects 5000)")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open the browser")
    parser.add_argument("--debug", action="store_true", help="Enable Flask debug mode")
    args = parser.parse_args()

    port = args.port if args.port > 0 else find_free_port(5000)
    url = f"http://127.0.0.1:{port}"

    if not args.no_browser:
        t = threading.Thread(target=open_browser, args=(url, 1.2), daemon=True)
        t.start()

    run_server(port=port, debug=args.debug)


if __name__ == "__main__":
    main()
