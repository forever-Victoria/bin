"""豆包 ASR —— 大模型流式语音识别 V3（sauc/bigmodel），支持边说边识别。

事实（官方 docs 6561/1354869）：
  wss://openspeech.bytedance.com/api/v3/sauc/bigmodel
  鉴权（WebSocket 握手 HTTP 头）：
    X-Api-App-Key=APP ID, X-Api-Access-Key=Access Token
    X-Api-Resource-Id=volc.bigasr.sauc.duration（1.0 小时版）
    X-Api-Connect-Id=UUID
  协议：4 字节头二进制帧（见 services/_volc.py），gzip
  单包音频 100~200ms（16k/16bit/mono，200ms≈6400 字节性能最优）

实时对话：LISTENING 期间持续 feed 音频，服务端边收边识别，松手后 finish 取最终文本。
"""
from __future__ import annotations

import asyncio
import logging
import uuid

import websockets

from config import settings
from . import _volc
from .base import ASRService, ASRSession

log = logging.getLogger("bin.asr")

_ASR_RETRIES = 3


class DoubaoAsrSession(ASRSession):
    """一条 ASR 流式会话：start 建连 → feed 喂音频 → finish 取最终文本 → close。"""

    def __init__(self, url: str) -> None:
        self._url = url
        self._ws = None
        self._recv_task: asyncio.Task | None = None
        self._final_text = ""
        self._done = asyncio.Event()
        self._error: Exception | None = None
        self._closed = False

    def _headers(self) -> dict:
        return {
            "X-Api-App-Key": settings.asr_appid,
            "X-Api-Access-Key": settings.asr_access_token,
            "X-Api-Resource-Id": settings.asr_resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }

    def _payload(self) -> dict:
        return {
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

    async def start(self) -> None:
        last_exc: Exception | None = None
        for attempt in range(1, _ASR_RETRIES + 1):
            try:
                self._ws = await websockets.connect(
                    self._url, additional_headers=self._headers())
                break
            except (OSError, websockets.WebSocketException) as e:
                last_exc = e
                log.warning("ASR 建连第 %d 次失败：%s，重试", attempt, type(e).__name__)
                await asyncio.sleep(0.4 * attempt)
        else:
            raise RuntimeError(f"ASR 建连失败：{type(last_exc).__name__}: {last_exc}")

        await self._ws.send(_volc.build_full_request(self._payload()))
        self._recv_task = asyncio.create_task(self._receive())

    async def _receive(self) -> None:
        try:
            async for raw in self._ws:
                frame = _volc.parse_server_frame(raw)
                if frame.is_error:
                    self._error = RuntimeError(
                        f"ASR 错误 {frame.error_code}: {frame.error_msg}")
                    self._done.set()
                    return
                if frame.is_json:
                    obj = frame.json()
                    text = (obj.get("result") or {}).get("text")
                    if text:
                        self._final_text = text
                if frame.is_last:
                    self._done.set()
                    return
        except Exception as e:  # noqa: BLE001
            if not self._closed:
                self._error = e
            self._done.set()

    async def feed(self, pcm: bytes) -> None:
        if self._ws and not self._closed and pcm:
            await self._ws.send(_volc.build_audio_request(pcm, is_last=False))

    async def finish(self) -> str:
        if self._ws and not self._closed:
            await self._ws.send(_volc.build_audio_request(b"", is_last=True))
        try:
            await asyncio.wait_for(self._done.wait(),
                                   timeout=settings.transcript_wait_sec)
        except asyncio.TimeoutError:
            if self._error is None:
                self._error = RuntimeError("ASR 收尾超时")
        if self._error:
            raise self._error
        log.info("ASR 转写: %s", self._final_text[:80])
        return self._final_text.strip()

    async def close(self) -> None:
        self._closed = True
        self._done.set()  # 唤醒可能仍在等待的 finish
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except Exception:  # noqa: BLE001
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass


class DoubaoAsrService(ASRService):
    def __init__(self) -> None:
        self._url = settings.asr_ws_url

    def session(self) -> ASRSession:
        return DoubaoAsrSession(self._url)
