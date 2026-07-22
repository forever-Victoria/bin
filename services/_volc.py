"""火山引擎 openspeech 二进制帧编解码（ASR bigmodel 与 TTS V1 共用）。

协议（官方文档 docs 6561/1354869、79821，整数一律大端）：
  4 字节头 | [可选 sequence 4B] | payload-size 4B | payload

  头字节（每半字节 4 bit）：
    byte0: protocol_version(0001) | header_size(0001，即 1×4=4 字节)
    byte1: message_type | type_specific_flags
    byte2: serialization | compression
    byte3: reserved 0x00

  message_type: 0001 full-client-request · 0010 audio-only-request
                1001 full-server-response · 1011 audio-only-response · 1111 error
  flags: bit0=含 sequence(number) · bit1=最后一包(负包)
         seq_present = bool(flags & 0b0001)；is_last = bool(flags & 0b0010)
  serialization: 0000 raw · 0001 JSON
  compression:  0000 none · 0001 gzip
"""
from __future__ import annotations

import gzip
import json
import struct
from dataclasses import dataclass

# message types
MT_FULL_CLIENT_REQUEST = 0x1
MT_AUDIO_ONLY_REQUEST = 0x2
MT_FULL_SERVER_RESPONSE = 0x9
MT_AUDIO_ONLY_RESPONSE = 0xB
MT_ERROR = 0xF

# serialization
SER_RAW = 0x0
SER_JSON = 0x1
# compression
COMP_NONE = 0x0
COMP_GZIP = 0x1


def _header(msg_type: int, flags: int, serial: int, comp: int) -> bytes:
    return bytes([0x11, (msg_type << 4) | flags, (serial << 4) | comp, 0x00])


def build_full_request(payload: dict) -> bytes:
    """构造 full-client-request 帧（JSON + gzip）。"""
    header = _header(MT_FULL_CLIENT_REQUEST, 0b0000, SER_JSON, COMP_GZIP)
    body = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return header + struct.pack(">I", len(body)) + body


def build_audio_request(pcm: bytes, is_last: bool = False) -> bytes:
    """构造 audio-only-request 帧（raw + gzip）。"""
    flags = 0b0010 if is_last else 0b0000
    header = _header(MT_AUDIO_ONLY_REQUEST, flags, SER_RAW, COMP_GZIP)
    body = gzip.compress(pcm)
    return header + struct.pack(">I", len(body)) + body


@dataclass
class ServerFrame:
    msg_type: int
    flags: int
    serial: int
    compression: int
    sequence: int | None
    is_last: bool
    payload: bytes          # 已解压
    error_code: int | None
    error_msg: str | None

    @property
    def is_json(self) -> bool:
        return self.serial == SER_JSON

    @property
    def is_error(self) -> bool:
        return self.msg_type == MT_ERROR

    def json(self) -> dict:
        return json.loads(self.payload.decode("utf-8")) if self.payload else {}


def parse_server_frame(buf: bytes) -> ServerFrame:
    if len(buf) < 4:
        raise ValueError(f"服务端帧过短: {len(buf)} bytes")
    header_size = (buf[0] & 0x0F) * 4
    if header_size < 4 or len(buf) < header_size:
        raise ValueError(f"服务端帧头长度无效: {header_size} bytes")
    msg_type = buf[1] >> 4
    flags = buf[1] & 0x0F
    serial = buf[2] >> 4
    comp = buf[2] & 0x0F

    if msg_type == MT_ERROR:
        offset = header_size
        if len(buf) < offset + 8:
            raise ValueError("错误帧不完整")
        code = struct.unpack(">I", buf[offset:offset + 4])[0]
        msg_size = struct.unpack(">I", buf[offset + 4:offset + 8])[0]
        offset += 8
        if len(buf) < offset + msg_size:
            raise ValueError("错误帧消息不完整")
        message = buf[offset:offset + msg_size]
        if comp == COMP_GZIP and message:
            message = gzip.decompress(message)
        msg = message.decode("utf-8", errors="replace")
        return ServerFrame(msg_type, flags, serial, comp, None, False, b"",
                           code, msg)

    offset = header_size
    sequence: int | None = None
    if flags & 0b0001:                       # 含 sequence number
        if len(buf) < offset + 4:
            raise ValueError("服务端帧缺少 sequence")
        sequence = struct.unpack(">i", buf[offset:offset + 4])[0]
        offset += 4
    if len(buf) < offset + 4:
        raise ValueError("服务端帧缺少 payload size")
    payload_size = struct.unpack(">I", buf[offset:offset + 4])[0]
    offset += 4
    if len(buf) < offset + payload_size:
        raise ValueError("服务端帧 payload 不完整")
    payload = buf[offset:offset + payload_size]
    if comp == COMP_GZIP and payload:
        payload = gzip.decompress(payload)
    is_last = bool(flags & 0b0010)
    return ServerFrame(msg_type, flags, serial, comp, sequence, is_last,
                       payload, None, None)
