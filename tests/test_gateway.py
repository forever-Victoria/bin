from __future__ import annotations

import json
import unittest

import gateway


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, payload: str) -> None:
        self.sent.append(payload)


class GatewayHeartbeatTest(unittest.IsolatedAsyncioTestCase):
    async def test_heartbeat_is_acknowledged(self) -> None:
        websocket = FakeWebSocket()

        await gateway._on_text(None, websocket, '{"type":"heartbeat"}')

        self.assertEqual(1, len(websocket.sent))
        self.assertEqual({"type": "heartbeat"}, json.loads(websocket.sent[0]))
