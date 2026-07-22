"""对话引擎：状态机 + 豆包三件套编排。

镜像 ljt OmniRealtimeBridge 的 Phase（IDLE/LISTENING/PROCESSING/SPEAKING），
语音引擎换成豆包 ASR→LLM→TTS 分体。

实时优化：
  - ASR 边说边识别：LISTENING 期间把音频实时喂进 ASR 会话，松手即得文本
    （ASR 工作摊到说话期间，不再占松手后的时间）。
  - LLM 流式 + 首句即合成：关 thinking，吐出第一句就送 TTS。
MVP 半双工；全双工 barge-in 为后续增强（见 frames.InterruptionFrame）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from enum import Enum
from typing import Awaitable, Callable

import messages as M
import transcript_filter
from roles import VoiceRole
from services import ASRService, LLMService, TTSService

log = logging.getLogger("bin.conv")

# 流式分句：LLM 一吐出完整句子就立刻送 TTS，不必等整段生成完（降首音延迟）
_SENT_RE = re.compile(r"[。！？!?…\n]")
_MAX_NO_PUNCT = 40  # 没有句末标点时，攒到此字数也先送合成，避免干等


def _take_sentence(buf: str) -> tuple[str | None, str]:
    """从 buf 取出第一个完整句子（含句末标点）；没有则返回 (None, buf)。"""
    m = _SENT_RE.search(buf)
    if m:
        return buf[:m.end()], buf[m.end():]
    if len(buf) >= _MAX_NO_PUNCT:
        return buf, ""
    return None, buf


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
        self._history: list[dict] = []
        self._lock = asyncio.Lock()
        self._asr_sess = None  # 实时 ASR 会话（LISTENING 期间存活）

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
            self._phase = Phase.LISTENING
        # 提前建好 ASR 流式会话：说话时的音频直接喂进去，边说边识别
        try:
            self._asr_sess = self._asr.session()
            await self._asr_sess.start()
            self._log("进入 LISTENING（ASR 实时识别中）")
        except Exception as e:  # noqa: BLE001
            what = type(e).__name__
            self._log(f"ASR 建连失败: {what}")
            await self._send_text(M.error(f"ASR 建连失败: {what}"))
            await self._close_asr()
            async with self._lock:
                self._phase = Phase.IDLE

    async def on_audio(self, pcm: bytes) -> None:
        # LISTENING 时把音频实时喂给 ASR（边说边识别），不再缓存
        if self._phase == Phase.LISTENING and self._asr_sess and pcm:
            try:
                await self._asr_sess.feed(pcm)
            except Exception as e:  # noqa: BLE001
                log.debug("ASR feed 失败: %s", e)

    async def on_listen_end(self) -> None:
        async with self._lock:
            if self._phase != Phase.LISTENING:
                self._log(f"忽略 listen_end（当前 {self._phase.value}）")
                return
            self._phase = Phase.PROCESSING

        t0 = time.monotonic()  # 松手时刻（用户体感的起点）
        text = ""
        try:
            if self._asr_sess:
                text = await self._asr_sess.finish()
        except Exception as e:  # noqa: BLE001
            what = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            self._log(f"ASR 收尾失败: {what}")
        finally:
            await self._close_asr()

        self._log(f"[时延] ASR 收尾 {time.monotonic() - t0:.2f}s（松手后）")
        if text:
            await self._send_text(M.transcript("user", text))
            self._log(f"用户: {text}")

        if not transcript_filter.has_meaningful_speech(text):
            await self._send_text(M.round_skip("未识别到有效文字（可能是噪声）"))
            async with self._lock:
                self._phase = Phase.IDLE
            return

        await self._reply(text, t0)

    async def on_cancel(self) -> None:
        await self._close_asr()
        async with self._lock:
            if self._phase != Phase.IDLE:
                self._log("取消，回到 IDLE")
            self._phase = Phase.IDLE

    async def close(self) -> None:
        """连接断开时清理 ASR 会话。"""
        await self._close_asr()

    async def _close_asr(self) -> None:
        if self._asr_sess is not None:
            try:
                await self._asr_sess.close()
            except Exception:  # noqa: BLE001
                pass
            self._asr_sess = None

    async def _reply(self, text: str, t0: float) -> None:
        """text 已转写好，执行 LLM 流式 + 首句即合成。t0=松手时刻，用于时延统计。"""
        t_first_token = t_first_audio = None
        try:
            async with self._lock:
                self._phase = Phase.SPEAKING
            await self._send_text(M.tts_start())

            buf = ""
            full = ""
            async for delta in self._llm.reply_stream(
                self.role.instructions, self._history, text
            ):
                if t_first_token is None:
                    t_first_token = time.monotonic()
                    self._log(f"[时延] LLM 首 token {t_first_token - t0:.2f}s（松手后）")
                full += delta
                buf += delta
                # 已完成的句子立刻送 TTS，不等整段生成完
                while True:
                    sentence, buf = _take_sentence(buf)
                    if sentence is None:
                        break
                    if sentence.strip():
                        async for pcm in self._tts.synthesize(sentence, self.role.speaker):
                            if t_first_audio is None:
                                t_first_audio = time.monotonic()
                                self._log(f"[时延] 首音（松手→出声）{t_first_audio - t0:.2f}s")
                            await self._send_bytes(pcm)

            if full.strip():
                await self._send_text(M.transcript("assistant", full.strip()))
                self._log(f"助手: {full.strip()}")
                self._history.append({"role": "user", "content": text})
                self._history.append({"role": "assistant", "content": full.strip()})

            # 收尾：最后一段没有句末标点的文本
            if buf.strip():
                async for pcm in self._tts.synthesize(buf, self.role.speaker):
                    await self._send_bytes(pcm)

            await self._send_text(M.tts_end())
            self._log(f"[时延] 总耗时 {time.monotonic() - t0:.2f}s")
            self._log("本轮回复完成 → IDLE")
        except Exception as e:  # noqa: BLE001
            what = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            self._log(f"处理异常: {what}")
            log.exception("处理失败")
            try:
                await self._send_text(M.error(f"处理失败: {what}"))
            except Exception:  # noqa: BLE001
                pass
        finally:
            async with self._lock:
                self._phase = Phase.IDLE
