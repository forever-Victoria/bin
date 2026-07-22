from __future__ import annotations

import gzip
import struct
import unittest

from services import _volc


class VolcServerFrameTest(unittest.TestCase):
    def test_parses_gzip_error_message(self) -> None:
        message = gzip.compress("illegal input text!".encode("utf-8"))
        frame_bytes = bytes([0x11, 0xF0, 0x01, 0x00])
        frame_bytes += struct.pack(">II", 3011, len(message)) + message

        frame = _volc.parse_server_frame(frame_bytes)

        self.assertTrue(frame.is_error)
        self.assertEqual(3011, frame.error_code)
        self.assertEqual("illegal input text!", frame.error_msg)

    def test_rejects_truncated_error_message(self) -> None:
        frame_bytes = bytes([0x11, 0xF0, 0x00, 0x00])
        frame_bytes += struct.pack(">II", 3011, 10) + b"short"

        with self.assertRaisesRegex(ValueError, "错误帧消息不完整"):
            _volc.parse_server_frame(frame_bytes)


if __name__ == "__main__":
    unittest.main()
