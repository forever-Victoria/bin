"""豆包 LLM —— 火山方舟 Ark（OpenAI 兼容）。

事实（官方 docs 82379）：
  base_url = https://ark.cn-beijing.volces.com/api/v3
  鉴权 = Authorization: Bearer <ARK_API_KEY>
  model  = doubao Model ID（如 doubao-seed-1-6-flash-250828）或 Endpoint ID（ep-xxx）
  流式 = stream=True，读 choices[0].delta.content
"""
from __future__ import annotations

import logging

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

    async def reply(self, system: str, history: list[dict], user_text: str) -> str:
        messages: list[dict] = [{"role": "system", "content": system}]
        messages.extend(history[-MAX_HISTORY_TURNS * 2:])
        messages.append({"role": "user", "content": user_text})

        resp = await self._client.chat.completions.create(
            model=settings.ark_model,
            messages=messages,
            max_tokens=settings.llm_max_tokens,
            temperature=settings.llm_temperature,
            stream=False,
        )
        text = (resp.choices[0].message.content or "").strip()
        log.info("LLM 回复: %s", text[:80])
        return text
