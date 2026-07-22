"""对话引擎：状态机 + 豆包三件套编排。

镜像 ljt OmniRealtimeBridge 的 Phase（IDLE/LISTENING/PROCESSING/SPEAKING），
但语音引擎从「百炼 Omni 端到端」换成「豆包 ASR→LLM→TTS 分体」。
MVP 为半双工：播放期间不开新一轮；全双工 barge-in 为后续增强（见 frames.InterruptionFrame）。
"""
from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import AsyncIterator, Callable, Awaitable

import messages as M
import transcript_filter
from roles import VoiceRole
from services import ASRService, LLMService, TTSService

log = logging.getLogger("bin.conv")

# 16k/16bit/mono 下 200ms = 6400 字节（ASR 单包最优大小）
ASR_FRAME_BYTES = 6400

SendText = Callable[[str], Awaitable[None]]
SendBytes = Callable[[bytes], Awaitable[None]]
LogFn = Callable[[str], None]


class Phase(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    SPEAKING = "SPEAKING"


class Conversation:
    def __init__(
        self,
        role: VoiceRole,
        send_text: SendText,
        send_bytes: SendBytes,
        logger: LogFn,
        asr: ASRService,
        llm: LLMService,
        tts: TTSService,
    ) -> None:
        self.role = role
        self._send_text = send_text
        self._send_bytes = send_bytes
        self._log = logger
        self._asr = asr
        self._llm = llm
        self._tts = tts
        self._phase = Phase.IDLE
        self._buffer: list[bytes] = []
        self._history: list[dict] = []
        self._lock = asyncio.Lock()

    @property
    def phase(self) -> Phase:
        return self._phase

    async def switch_role(self, role: VoiceRole) -> bool:
        async with self._lock:
            if self._phase != Phase.IDLE:
                return False
            self.role = role
            self._log(f"已切换角色: {role.id} ({role.display_name})")
            return True

    async def on_listen_start(self) -> None:
        async with self._lock:
            if self._phase == Phase.LISTENING:
                return
            if self._phase != Phase.IDLE:
                self._log(f"忽略 listen_start（当前 {self._phase.value}）")
                return
            self._buffer.clear()
            self._phase = Phase.LISTENING
            self._log("进入 LISTENING")

    async def on_audio(self, pcm: bytes) -> None:
        if self._phase == Phase.LISTENING and pcm:
            self._buffer.append(pcm)

    async def on_listen_end(self) -> None:
        async with self._lock:
            if self._phase != Phase.LISTENING:
                self._log(f"忽略 listen_end（当前 {self._phase.value}）")
                return
            self._phase = Phase.PROCESSING
        # 锁已释放，长时间 ASR/LLM/TTS 不阻塞状态切换
        await self._process()

    async def on_cancel(self) -> None:
        async with self._lock:
            self._buffer.clear()
            if self._phase != Phase.IDLE:
                self._log("取消，回到 IDLE")
            self._phase = Phase.IDLE

    async def _chunked(self, audio: bytes) -> AsyncIterator[bytes]:
        for i in range(0, len(audio), ASR_FRAME_BYTES):
            yield audio[i:i + ASR_FRAME_BYTES]

    async def _process(self) -> None:
        try:
            audio = b"".join(self._buffer)
            self._buffer.clear()

            text = await self._asr.transcribe(self._chunked(audio))
            if text:
                await self._send_text(M.transcript("user", text))
                self._log(f"用户: {text}")

            if not transcript_filter.has_meaningful_speech(text):
                await self._send_text(M.round_skip("未识别到有效文字（可能是噪声）"))
                return

            reply = await self._llm.reply(self.role.instructions, self._history, text)
            await self._send_text(M.transcript("assistant", reply))
            self._log(f"助手: {reply}")
            self._history.append({"role": "user", "content": text})
            self._history.append({"role": "assistant", "content": reply})

            async with self._lock:
                self._phase = Phase.SPEAKING
            await self._send_text(M.tts_start())
            async for pcm in self._tts.synthesize(reply, self.role.speaker):
                await self._send_bytes(pcm)
            await self._send_text(M.tts_end())
            self._log("本轮回复完成 → IDLE")
        except Exception as e:  # noqa: BLE001
            what = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            self._log(f"处理异常: {what}")
            log.exception("处理失败")
            try:
                await self._send_text(M.error(f"处理失败: {what}"))
            except Exception:
                pass
        finally:
            async with self._lock:
                self._phase = Phase.IDLE
