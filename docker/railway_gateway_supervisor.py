#!/usr/bin/env python3
"""Railway supervisor for Telegram-only Hermes deployments.

Runs a tiny HTTP readiness endpoint on ``$PORT`` for Railway while supervising
the Hermes gateway process. ``/health`` returns 200 only after the gateway
reports ``gateway_state=running`` in the persisted runtime status file for the
current startup. All other paths return 404.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from email.utils import format_datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional


HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
STATUS_PATH = HERMES_HOME / "gateway_state.json"
PID_PATH = HERMES_HOME / "gateway.pid"
PORT = int(os.environ.get("PORT", "8080"))
STARTED_AT = datetime.now(timezone.utc)

_child_lock = threading.Lock()
_child: Optional[subprocess.Popen[str]] = None


def _read_status() -> Optional[dict]:
    try:
        payload = json.loads(STATUS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _status_ready(payload: Optional[dict]) -> bool:
    if not payload or payload.get("gateway_state") != "running":
        return False

    updated_at = payload.get("updated_at")
    if not isinstance(updated_at, str):
        return False

    try:
        updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return False

    return updated >= STARTED_AT


def _child_running() -> bool:
    with _child_lock:
        return _child is not None and _child.poll() is None


def _send_signal(sig: int) -> None:
    with _child_lock:
        child = _child
    if child is None or child.poll() is not None:
        return
    try:
        child.send_signal(sig)
    except ProcessLookupError:
        pass


class _HealthHandler(BaseHTTPRequestHandler):
    server_version = "HermesRailwayHealth/1.0"

    def do_GET(self) -> None:
        if self.path != "/health":
            self._respond(HTTPStatus.NOT_FOUND, b"not found\n")
            return

        ready = _child_running() and _status_ready(_read_status())
        code = HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE
        body = (b'{"status":"ok"}\n' if ready else b'{"status":"starting"}\n')
        self._respond(code, body)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _respond(self, status: HTTPStatus, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Date", format_datetime(datetime.now(timezone.utc), usegmt=True))
        self.end_headers()
        self.wfile.write(body)


def _clear_stale_status() -> None:
    for path in (STATUS_PATH, PID_PATH):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def main() -> int:
    _clear_stale_status()

    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), _HealthHandler)
    httpd.daemon_threads = True
    server_thread = threading.Thread(target=httpd.serve_forever, name="railway-health", daemon=True)
    server_thread.start()

    def _handle_signal(sig: int, _frame: object) -> None:
        _send_signal(sig)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    child = subprocess.Popen(
        ["/opt/hermes/docker/entrypoint.sh", "gateway"],
        text=True,
    )
    with _child_lock:
        global _child
        _child = child

    try:
        return child.wait()
    finally:
        httpd.shutdown()
        httpd.server_close()
        server_thread.join(timeout=2)


if __name__ == "__main__":
    sys.exit(main())
