"""FastAPI HTTP/WebSocket server for Stomphacks + zoom-faust-fx."""

from __future__ import annotations

__all__ = ["create_app", "main"]


def create_app():
    from .app import create_app as _create_app

    return _create_app()


def main() -> int:
    from .app import main as _main

    return _main()
