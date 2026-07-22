"""服务抽象基类。

把「语音引擎」与「协议/状态机」解耦：conversation.py 只依赖这些抽象，
具体实现（豆包 ASR/LLM/TTS）可替换。换 V3 TTS、换别家 ASR 都不改引擎。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator


class ASRSession(ABC):
    """流式 ASR 会话：边说边识别。

    生命周期：start（建连+发元数据）→ feed（实时喂音频帧，可多次）→
    finish（发结束包，取最终文本）→ close。
    """

    @abstractmethod
    async def start(self) -> None:
        ...

    @abstractmethod
    async def feed(self, pcm: bytes) -> None:
        ...

    @abstractmethod
    async def finish(self) -> str:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...


class ASRService(ABC):
    """语音识别服务。实时对话用 session() 拿流式会话。"""

    @abstractmethod
    def session(self) -> ASRSession:
        ...

    async def transcribe(self, audio_chunks: AsyncIterator[bytes]) -> str:
        """批量识别（便捷封装）：内部用流式会话实现。"""
        sess = self.session()
        await sess.start()
        try:
            async for chunk in audio_chunks:
                if chunk:
                    await sess.feed(chunk)
            return await sess.finish()
        finally:
            await sess.close()


class LLMService(ABC):
    """对话大模型：流式产出回复 token（首句即合成，降低对话延迟）。"""

    @abstractmethod
    def reply_stream(self, system: str, history: list[dict], user_text: str) -> AsyncIterator[str]:
        ...

    async def reply(self, system: str, history: list[dict], user_text: str) -> str:
        """非流式便捷封装：聚合整段回复。"""
        parts: list[str] = []
        async for delta in self.reply_stream(system, history, user_text):
            parts.append(delta)
        return "".join(parts).strip()


class TTSService(ABC):
    """流式语音合成：给定文本与音色，产出 PCM 分片（24k/16bit/mono）。"""

    @abstractmethod
    def synthesize(self, text: str, speaker: str) -> AsyncIterator[bytes]:
        ...
