"""服务抽象基类。

把「语音引擎」与「协议/状态机」解耦：conversation.py 只依赖这些抽象，
具体实现（豆包 ASR/LLM/TTS）可替换。换 V3 TTS、换别家 ASR 都不改引擎。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator


class ASRService(ABC):
    """流式语音识别：喂入音频分片，返回完整转写文本。"""

    @abstractmethod
    async def transcribe(self, audio_chunks: AsyncIterator[bytes]) -> str:
        ...


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
