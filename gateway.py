"""设备 WebSocket 网关：1:1 复刻 ljt 协议，对接现有 ESP32 固件。

协议见 ljt/docs/设备WebSocket接口.md：
  连接 ws://host:port?device_id=xxx&role_id=xxx
  上行 Text JSON: listen_start/listen_end/cancel/set_role/heartbeat
  上行 Binary: PCM 16k/16bit/mono
  下行 Text JSON: ready/role_changed/tts_start/tts_end/barge_in/transcript/round_skip/error
  下行 Binary: PCM 24k/16bit/mono
  相同 device_id 踢线：旧连接收 error + 关闭码 4001
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import WebSocket

import messages as M
from config import settings
from conversation import Conversation
from roles import resolve_role, require_role
from services import DoubaoAsrService, DoubaoLlmService, DoubaoTtsV1Service

log = logging.getLogger("bin.gateway")

CLOSE_REPLACED = 4001

# 单例服务（无状态，跨设备共享；每次识别/合成各自建 WS）
_asr: Optional[DoubaoAsrService] = None
_llm: Optional[DoubaoLlmService] = None
_tts: Optional[DoubaoTtsV1Service] = None

# device_id → 当前活跃连接（踢线用）
_connections: dict[str, WebSocket] = {}


def init_services() -> list[str]:
    """创建服务单例；返回配置告警列表。"""
    global _asr, _llm, _tts
    _asr = DoubaoAsrService()
    _llm = DoubaoLlmService()
    _tts = DoubaoTtsV1Service()
    return settings.sanity_check()


async def _kick_existing(device_id: str, new_ws: WebSocket) -> None:
    old = _connections.get(device_id)
    if old is None or old is new_ws:
        return
    log.info("重复 device_id，踢下线旧连接: %s", device_id)
    try:
        await old.send_text(M.error("设备已在别处上线，本连接被断开"))
        await old.close(code=CLOSE_REPLACED, reason="replaced_by_new_connection")
    except Exception:  # noqa: BLE001
        pass


async def handle_device(ws: WebSocket, device_id: str, role_id: str | None) -> None:
    await _kick_existing(device_id, ws)
    _connections[device_id] = ws

    role = resolve_role(role_id)

    def logger(msg: str) -> None:
        log.info("[设备 %s] %s", device_id, msg)

    conv = Conversation(
        role=role,
        send_text=ws.send_text,
        send_bytes=ws.send_bytes,
        logger=logger,
        asr=_asr,
        llm=_llm,
        tts=_tts,
    )

    try:
        await ws.send_text(M.ready(role.id, role.display_name, settings.barge_in_enabled))
        logger(f"已就绪 角色={role.id}({role.display_name}) | 当前在线 {len(_connections)} 台")

        while True:
            msg = await ws.receive()
            mtype = msg.get("type")
            if mtype == "websocket.disconnect":
                break
            text = msg.get("text")
            if text is not None:
                await _on_text(conv, ws, text)
                continue
            data = msg.get("bytes")
            if data is not None:
                await conv.on_audio(data)
    except Exception as e:  # noqa: BLE001
        logger(f"连接异常: {e}")
    finally:
        await conv.close()  # 关掉可能残留的实时 ASR 会话
        if _connections.get(device_id) is ws:
            _connections.pop(device_id, None)
        logger(f"断开 | 当前在线 {len(_connections)} 台")


async def _on_text(conv: Conversation, ws: WebSocket, text: str) -> None:
    obj = M.parse(text)
    t = M.type_of(obj)
    if t == "":
        await ws.send_text(M.error("忽略非 JSON 控制文本帧"))
        return
    if t == M.LISTEN_START:
        await conv.on_listen_start()
    elif t == M.LISTEN_END:
        await conv.on_listen_end()
    elif t == M.CANCEL:
        await conv.on_cancel()
    elif t == M.SET_ROLE:
        await _handle_set_role(conv, ws, obj.get("role_id", ""))
    elif t == M.HEARTBEAT:
        pass
    else:
        await ws.send_text(M.error(f"未知消息: {t}"))


async def _handle_set_role(conv: Conversation, ws: WebSocket, role_id: str) -> None:
    if not role_id:
        await ws.send_text(M.error("set_role 缺少 role_id"))
        return
    try:
        role = require_role(role_id)
    except KeyError:
        await ws.send_text(M.error(f"未知角色: {role_id}"))
        return
    if await conv.switch_role(role):
        await ws.send_text(M.role_changed(role.id, role.display_name))
    else:
        await ws.send_text(M.error("当前正在对话或播报，无法切换角色"))
