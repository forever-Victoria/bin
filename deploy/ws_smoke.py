"""Minimal WebSocket readiness check for a deployed bin gateway."""
from __future__ import annotations

import json
import sys

from websockets.sync.client import connect


url = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8767/?device_id=deploy-check"
with connect(url, open_timeout=8) as websocket:
    message = json.loads(websocket.recv(timeout=8))
    if message.get("type") != "ready":
        raise RuntimeError(f"expected ready, got {message.get('type')!r}")
    print(
        "WS_READY",
        f"role={message.get('role_id')}",
        f"barge_in={message.get('barge_in')}",
    )
