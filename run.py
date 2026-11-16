#!/usr/bin/env python3
"""Start the GitHub Gmail Finder web app:  python3 run.py"""

import argparse
import threading
import webbrowser

from backend.server import make_server


def main() -> None:
    p = argparse.ArgumentParser(description="Run the GitHub Gmail Finder web app.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true", help="Don't open a browser tab")
    args = p.parse_args()

    server = make_server(args.host, args.port)
    url = f"http://{args.host}:{args.port}/"
    print(f"GitHub Gmail Finder running at {url}  (Ctrl+C to stop)")
    if not server.has_token:
        print("No GitHub token saved yet. Enter your token in the browser page to start.")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
