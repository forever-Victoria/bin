"""豆包 TTS —— 语音合成 V1（ws_binary）。

MVP 选 V1：协议成熟、Python 示例多、可靠。支持预置音色（BVxxx_streaming）与
复刻 1.0 音色（S_xxx）。复刻 2.0 / 音色设计 / expressive 需要 V3 双向流式
（接口见 TTSService 抽象，后续用 DoubaoTtsV3Service 替换即可，引擎不改）。

事实（官方 docs 6561/79821、79823）：
  wss://openspeech.bytedance.com/api/v1/tts/ws_binary
  鉴权：Authorization: Bearer; {token}（注意是分号）；body 内 app{appid,token,cluster}
  请求 JSON：app/user/audio/request，audio.voice_type=音色，audio.encoding=pcm
  协议：4 字节头二进制帧（见 services/_volc.py）
  下行：frontier(0b1001 JSON) → audio(0b1011 PCM) → 末包 is_last
"""
from __future__ import annotations

import logging
import uuid
from typing import AsyncIterator

import websockets

from config import settings
from . import _volc
from .base import TTSService

log = logging.getLogger("bin.tts")


class DoubaoTtsV1Service(TTSService):
    def __init__(self) -> None:
        self._url = settings.tts_ws_url
        self._appid = settings.tts_appid
        self._token = settings.tts_access_token

    async def synthesize(self, text: str, speaker: str) -> AsyncIterator[bytes]:
        if not text.strip():
            return
        headers = {"Authorization": f"Bearer; {self._token}"}
        voice = speaker or settings.tts_voice_default

        payload = {
            "app": {
                "appid": self._appid,
                "token": self._token,
                "cluster": settings.tts_cluster,
            },
            "user": {"uid": "bin-gateway"},
            "audio": {
                "voice_type": voice,
                "encoding": "pcm",
                "rate": settings.tts_sample_rate,
                "speed_ratio": settings.tts_speed_ratio,
                "volume": settings.tts_volume,
                "pitch_ratio": 1.0,
            },
            "request": {
                "reqid": str(uuid.uuid4()),
                "text": text,
                "operation": "submit",
                "with_frontend": 1,
                "frontend_type": "unitTson",
            },
        }

        async with websockets.connect(self._url, additional_headers=headers) as ws:
            await ws.send(_volc.build_full_request(payload))
            async for raw in ws:
                frame = _volc.parse_server_frame(raw)
                if frame.is_error:
                    raise RuntimeError(
                        f"TTS 错误 {frame.error_code}: {frame.error_msg}")
                # 只下发音频帧（PCM 24k/16bit/mono）
                if frame.msg_type == _volc.MT_AUDIO_ONLY_RESPONSE and frame.payload:
                    yield frame.payload
                if frame.is_last:
                    break
        log.info("TTS 合成完成: voice=%s text=%s", voice, text[:40])
