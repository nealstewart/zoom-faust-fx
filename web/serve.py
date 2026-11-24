#!/usr/bin/env python3
"""
web/serve.py — Local dev server for the Stomphacks ZD2 WebAssembly Auditioner.

Ensures `.wasm` and `.js` are served with correct MIME types and disables
caching so rebuilt effects appear immediately on reload.
"""

from __future__ import annotations

import argparse
import http.server
import mimetypes
import socketserver
import subprocess
import sys
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent
DIST_JSON = WEB_DIR / "dist" / "effects.json"

mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("audio/wav", ".wav")


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        super().end_headers()


class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve Stomphacks WebAssembly Auditioner")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild all WASM modules before serving")
    args = parser.parse_args()

    if args.rebuild or not DIST_JSON.is_file():
        print("Building WebAssembly effect modules...")
        subprocess.run([sys.executable, str(WEB_DIR / "build_wasm.py")], check=True)

    url = f"http://{args.host}:{args.port}/"
    print(f"\nStomphacks ZD2 WebAssembly Auditioner running at {url}")
    print("Press Ctrl+C to stop.\n")

    with ReusableTCPServer((args.host, args.port), NoCacheHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
