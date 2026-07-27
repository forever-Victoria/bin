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
    def test_downlink_rate_negotiation(self) -> None:
        self.assertEqual(16_000, gateway.negotiate_downlink_rate("16000"))
        self.assertEqual(24_000, gateway.negotiate_downlink_rate("24000"))
        self.assertEqual(24_000, gateway.negotiate_downlink_rate("44100"))
        self.assertEqual(24_000, gateway.negotiate_downlink_rate(None))

    async def test_heartbeat_is_acknowledged(self) -> None:
        websocket = FakeWebSocket()

        await gateway._on_text(None, websocket, '{"type":"heartbeat"}')

        self.assertEqual(1, len(websocket.sent))
        self.assertEqual({"type": "heartbeat"}, json.loads(websocket.sent[0]))
