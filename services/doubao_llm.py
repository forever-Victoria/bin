"""豆包 LLM —— 火山方舟 Ark（OpenAI 兼容），流式输出 token。

事实（官方 docs 82379）：
  base_url = https://ark.cn-beijing.volces.com/api/v3
  鉴权 = Authorization: Bearer <ARK_API_KEY>
  model  = doubao Model ID（如 doubao-seed-2-0-lite-260428）或 Endpoint ID（ep-xxx）
  流式 = stream=True，读 choices[0].delta.content

流式输出的 token 由 conversation 按句切分后立刻送 TTS，首句即开始合成播放，
不必等整段回复生成完——这是降低对话延迟的关键。
"""
from __future__ import annotations

import logging
from typing import AsyncIterator

from openai import AsyncOpenAI

from config import settings
from .base import LLMService

log = logging.getLogger("bin.llm")

# 最多保留的历史轮次（每轮含 user+assistant）
MAX_HISTORY_TURNS = 6


class DoubaoLlmService(LLMService):
    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            api_key=settings.ark_api_key,
            base_url=settings.ark_base_url,
        )

    async def reply_stream(
        self, system: str, history: list[dict], user_text: str
    ) -> AsyncIterator[str]:
        messages: list[dict] = [{"role": "system", "content": system}]
        messages.extend(history[-MAX_HISTORY_TURNS * 2:])
        messages.append({"role": "user", "content": user_text})

        stream = await self._client.chat.completions.create(
            model=settings.ark_model,
            messages=messages,
            max_tokens=settings.llm_max_tokens,
            temperature=settings.llm_temperature,
            stream=True,
            # 关闭豆包 seed 系列的「深度思考」：否则模型会先无声推理数秒才吐第一个字，
            # 语音对话首音延迟会飙到 8s+。语音场景要快，不要思考。
            extra_body={"thinking": {"type": "disabled"}},
        )
        async for chunk in stream:
            if chunk.choices:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
