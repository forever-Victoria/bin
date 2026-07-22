"""豆包 ASR —— 大模型流式语音识别 V3（sauc/bigmodel）。

事实（官方 docs 6561/1354869）：
  wss://openspeech.bytedance.com/api/v3/sauc/bigmodel
  鉴权（WebSocket 握手 HTTP 头）：
    X-Api-App-Key=APP ID, X-Api-Access-Key=Access Token
    X-Api-Resource-Id=volc.bigasr.sauc.duration（1.0 小时版）
    X-Api-Connect-Id=UUID
  协议：4 字节头二进制帧（见 services/_volc.py），gzip
  单包音频 200ms 最优（16k/16bit/mono ≈ 6400 字节）
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import AsyncIterator

import websockets

from config import settings
from . import _volc
from .base import ASRService

log = logging.getLogger("bin.asr")

_ASR_RETRIES = 3


class DoubaoAsrService(ASRService):
    def __init__(self) -> None:
        self._url = settings.asr_ws_url

    async def transcribe(self, audio_chunks: AsyncIterator[bytes]) -> str:
        # 缓冲成列表，便于失败时重试（async 迭代器是一次性的）
        chunks: list[bytes] = [c async for c in audio_chunks]
        last_exc: Exception | None = None
        for attempt in range(1, _ASR_RETRIES + 1):
            try:
                return await self._transcribe_once(self._aiter(chunks))
            except (OSError, websockets.WebSocketException) as e:
                last_exc = e
                log.warning("ASR 第 %d 次失败：%s，重试", attempt, type(e).__name__)
                await asyncio.sleep(0.4 * attempt)
        raise RuntimeError(
            f"ASR 重试 {_ASR_RETRIES} 次仍失败：{type(last_exc).__name__}: {last_exc}")

    @staticmethod
    async def _aiter(items: list[bytes]) -> AsyncIterator[bytes]:
        for x in items:
            yield x

    async def _transcribe_once(self, audio_chunks: AsyncIterator[bytes]) -> str:
        # 每次识别用独立连接与 Connect-Id，便于排错
        headers = {
            "X-Api-App-Key": settings.asr_appid,
            "X-Api-Access-Key": settings.asr_access_token,
            "X-Api-Resource-Id": settings.asr_resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }

        async with websockets.connect(self._url, additional_headers=headers) as ws:
            payload = {
                "user": {"uid": "bin-gateway"},
                "audio": {
                    "format": "pcm",
                    "rate": settings.asr_sample_rate,
                    "bits": 16,
                    "channel": 1,
                    "language": "zh-CN",
                },
                "request": {
                    "model_name": "bigmodel",
                    "enable_itn": True,
                    "enable_punc": True,
                    "result_type": "full",
                },
            }
            await ws.send(_volc.build_full_request(payload))

            final_text = ""

            async def sender() -> None:
                async for chunk in audio_chunks:
                    if chunk:
                        await ws.send(_volc.build_audio_request(chunk, is_last=False))
                # 最后一包：空负载 + 负包标志
                await ws.send(_volc.build_audio_request(b"", is_last=True))

            sender_task = asyncio.create_task(sender())
            try:
                async for raw in ws:
                    frame = _volc.parse_server_frame(raw)
                    if frame.is_error:
                        raise RuntimeError(
                            f"ASR 错误 {frame.error_code}: {frame.error_msg}")
                    if frame.is_json:
                        obj = frame.json()
                        text = (obj.get("result") or {}).get("text")
                        if text:
                            final_text = text
                    if frame.is_last:
                        break
            finally:
                if not sender_task.done():
                    sender_task.cancel()
                    try:
                        await sender_task
                    except (asyncio.CancelledError, Exception):
                        pass

            log.info("ASR 转写: %s", final_text[:80])
            return final_text.strip()
