"""设备 WebSocket JSON 协议（与 ljt/ESP32 固件 1:1 兼容）。

固件协议见 ljt/docs/设备WebSocket接口.md。字段名必须与 Java 版 protocol.DeviceMessage
完全一致，否则现有 ESP32 固件无法对接。
"""
from __future__ import annotations

import json
from typing import Any

# ── 设备 → 网关（上行 type）──────────────────────────────────────────────
LISTEN_START = "listen_start"
LISTEN_END = "listen_end"
CANCEL = "cancel"
SET_ROLE = "set_role"
HEARTBEAT = "heartbeat"
# 以下为全双工打断相关（固件 v2 用，MVP 暂不处理但保留解析）
PLAYBACK_PROGRESS = "playback_progress"
PLAYBACK_COMPLETE = "playback_complete"
BARGE_CANDIDATE = "barge_candidate"
BARGE_ACK = "barge_ack"
BARGE_VAD = "barge_vad"

# ── 网关 → 设备（下行 type）──────────────────────────────────────────────
READY = "ready"
ROLE_CHANGED = "role_changed"
TTS_START = "tts_start"
TTS_END = "tts_end"
BARGE_IN = "barge_in"
TRANSCRIPT = "transcript"
ROUND_SKIP = "round_skip"
ERROR = "error"


def _dumps(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def of(type_: str, **fields: Any) -> str:
    payload = {"type": type_}
    payload.update(fields)
    return _dumps(payload)


def ready(
    role_id: str, role_name: str, barge_in: bool, sample_rate: int = 24000
) -> str:
    return of(
        READY,
        role_id=role_id,
        role_name=role_name,
        barge_in=barge_in,
        sample_rate=sample_rate,
    )


def role_changed(role_id: str, role_name: str) -> str:
    return of(ROLE_CHANGED, role_id=role_id, role_name=role_name)


def heartbeat() -> str:
    return of(HEARTBEAT)


def tts_start(turn_id: int | None = None) -> str:
    return of(TTS_START, **({"turn_id": turn_id} if turn_id is not None else {}))


def tts_end(turn_id: int | None = None) -> str:
    return of(TTS_END, **({"turn_id": turn_id} if turn_id is not None else {}))


def barge_in(turn_id: int | None = None) -> str:
    return of(BARGE_IN, **({"turn_id": turn_id} if turn_id is not None else {}))


def transcript(role: str, text: str) -> str:
    return of(TRANSCRIPT, role=role, text=text)


def round_skip(reason: str) -> str:
    return of(ROUND_SKIP, reason=reason)


def error(message: str) -> str:
    return of(ERROR, message=message)


# ── 解析 ────────────────────────────────────────────────────────────────
def parse(text: str | bytes | None) -> dict[str, Any] | None:
    """解析上行 JSON 文本帧；非法/空返回 None。"""
    if text is None:
        return None
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not text or not text.strip():
        return None
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def type_of(obj: dict[str, Any] | None) -> str:
    return (obj or {}).get("type", "") or ""
