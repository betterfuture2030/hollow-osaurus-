#!/usr/bin/env python3
"""Operator panel launcher.

Opens the panel UI (served by the running habitat at /panel) in a native
pywebview window when pywebview is installed (pip3 install pywebview),
otherwise in the default browser. The habitat must be running.
"""

import json
import sys
import webbrowser
from pathlib import Path

import httpx

from substrate import DEFAULT_CONFIG, load_config


def main():
    cfg_path = Path(__file__).resolve().parent / "config.json"
    port = DEFAULT_CONFIG["runtime"]["api_port"]
    if cfg_path.exists():
        port = load_config(cfg_path)["runtime"]["api_port"]
    url = f"http://127.0.0.1:{port}/panel"

    try:
        httpx.get(f"http://127.0.0.1:{port}/health", timeout=3).raise_for_status()
    except httpx.HTTPError:
        print(f"habitat not reachable on port {port} — start it first: python3 hollow.py")
        sys.exit(1)

    try:
        import webview
    except ImportError:
        print("pywebview not installed (pip3 install pywebview); opening in browser")
        webbrowser.open(url)
        return
    webview.create_window("Hollow Operator Panel", url, width=1150, height=820)
    webview.start()


if __name__ == "__main__":
    main()
