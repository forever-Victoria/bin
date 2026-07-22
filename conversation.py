"""Conversation orchestration for streaming ASR -> LLM -> TTS.

The WebSocket receive loop must never be occupied by response generation: a
turn is finalized in a background task so microphone frames continue arriving
while the assistant speaks.  During SPEAKING those frames feed the barge-in
detector; an interruption cancels the response task, stops device playback and
starts a fresh streaming ASR session with retained near-end pre-roll.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from enum import Enum
from typing import Awaitable, Callable

from barge_in import BargeInConfig, BargeInDetector
from config import settings
import messages as M
import transcript_filter
from roles import VoiceRole
from services import ASRService, LLMService, TTSService
from services.base import ASRSession

log = logging.getLogger("bin.conv")

_SENT_RE = re.compile(r"[。！？!?…\n]")
_MAX_NO_PUNCT = 40
_MAX_BARGE_ACK_AUDIO_BYTES = 16_000 * 2 * 2
_MAX_SAFE_BARGE_ACK_MS = 1500


def _take_sentence(buf: str) -> tuple[str | None, str]:
    """Take one complete sentence, or flush a long unpunctuated fragment."""
    match = _SENT_RE.search(buf)
    if match:
        return buf[: match.end()], buf[match.end() :]
    if len(buf) >= _MAX_NO_PUNCT:
        return buf, ""
    return None, buf


def _has_speakable_text(text: str) -> bool:
    """Return whether a TTS fragment contains at least one letter or digit."""
    return any(char.isalnum() for char in text)


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
        barge_config: BargeInConfig | None = None,
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
        self._send_lock = asyncio.Lock()
        self._asr_sess: ASRSession | None = None
        self._turn_task: asyncio.Task[None] | None = None
        self._turn_sequence = 0
        self._active_tts_turn_id = 0
        self._interrupted_tts_turn_id = 0
        self._generation_complete = False
        self._pending_history: tuple[int, str, str] | None = None

        config = barge_config or BargeInConfig(
            enabled=settings.barge_in_enabled,
            rms_threshold=settings.barge_in_rms_threshold,
            hold_ms=settings.barge_in_hold_ms,
            pre_roll_ms=settings.barge_in_pre_roll_ms,
            echo_correlation_threshold=settings.barge_in_echo_correlation,
            echo_residual_rms=settings.barge_in_echo_residual_rms,
            min_residual_ratio=settings.barge_in_min_residual_ratio,
            reference_window_ms=settings.barge_in_reference_window_ms,
            startup_guard_ms=settings.barge_in_startup_guard_ms,
        )
        self._barge_config = config
        self._barge_detector = BargeInDetector(config)
        self._barge_listening = False
        self._awaiting_barge_ack = False
        self._barge_started_at = 0.0
        self._pending_barge_pre_roll = b""
        self._pending_barge_ack_audio = bytearray()
        self._last_barge_log_at = 0.0

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def active_tts_turn_id(self) -> int:
        return self._active_tts_turn_id

    async def switch_role(self, role: VoiceRole) -> bool:
        async with self._lock:
            if self._phase != Phase.IDLE:
                return False
            self.role = role
        self._log(f"已切换角色: {role.id} ({role.display_name})")
        return True

    async def on_listen_start(self) -> None:
        if self._phase == Phase.LISTENING:
            if self._awaiting_barge_ack:
                await self._complete_barge_handshake("listen_start")
            self._log("忽略重复 listen_start（已经在 LISTENING）")
            return

        if self._phase == Phase.SPEAKING and self._barge_config.enabled:
            # New firmware normally sends barge_candidate; listen_start stays
            # as a rolling-upgrade compatible explicit interruption signal.
            if self._generation_complete:
                await self._begin_listening_after_playback()
            else:
                await self._begin_barge_in(
                    self._barge_detector.snapshot_pre_roll(), "设备 listen_start"
                )
            return

        async with self._lock:
            if self._phase != Phase.IDLE:
                self._log(f"忽略 listen_start（当前 {self._phase.value}）")
                return
            self._phase = Phase.LISTENING
        await self._start_asr()

    async def _begin_listening_after_playback(self) -> None:
        async with self._lock:
            if self._phase != Phase.SPEAKING:
                return
            self._phase = Phase.LISTENING
            self._commit_pending_history(self._active_tts_turn_id)
            self._active_tts_turn_id = 0
            self._generation_complete = False
            self._barge_detector.reset()
        await self._start_asr()

    async def _start_asr(self) -> bool:
        session = self._asr.session()
        self._asr_sess = session
        try:
            await session.start()
            self._log("进入 LISTENING（ASR 实时识别中）")
            return True
        except Exception as exc:  # noqa: BLE001
            what = type(exc).__name__
            self._log(f"ASR 建连失败: {what}")
            await self._send_text_frame(M.error(f"ASR 建连失败: {what}"))
            await self._close_session(session)
            if self._asr_sess is session:
                self._asr_sess = None
            async with self._lock:
                self._phase = Phase.IDLE
            return False

    async def on_audio(self, pcm: bytes) -> None:
        if not pcm:
            return
        if self._phase == Phase.SPEAKING and self._barge_config.enabled:
            detection = self._barge_detector.accept(pcm)
            now = time.monotonic()
            if now - self._last_barge_log_at >= 0.5:
                self._last_barge_log_at = now
                decision = (
                    "BARGE_IN"
                    if detection.triggered
                    else "GUARD"
                    if detection.startup_guard
                    else "ECHO"
                    if detection.playback_echo
                    else "WAIT"
                )
                self._log(
                    "打断诊断 "
                    f"raw_rms={detection.rms} residual_rms={detection.residual_rms} "
                    f"ratio={detection.residual_ratio:.2f} corr={detection.correlation:.3f} "
                    f"delay={detection.delay_ms}ms decision={decision}"
                )
            if detection.triggered:
                self._log(
                    f"检测到用户打断，RMS={detection.rms}，"
                    f"保留预录 {len(detection.captured_audio)} bytes"
                )
                await self._begin_barge_in(detection.captured_audio, "服务端检测")
            return

        if self._phase != Phase.LISTENING:
            return
        if self._awaiting_barge_ack:
            self._pending_barge_ack_audio.extend(pcm)
            overflow = len(self._pending_barge_ack_audio) - _MAX_BARGE_ACK_AUDIO_BYTES
            if overflow > 0:
                del self._pending_barge_ack_audio[:overflow]
            return
        session = self._asr_sess
        if session is not None:
            try:
                await session.feed(pcm)
            except Exception as exc:  # noqa: BLE001
                log.debug("ASR feed 失败: %s", exc)

    async def on_listen_end(self) -> None:
        if self._phase != Phase.LISTENING:
            self._log(f"忽略 listen_end（当前 {self._phase.value}）")
            return
        if self._awaiting_barge_ack:
            await self._complete_barge_handshake("listen_end fallback")

        async with self._lock:
            if self._phase != Phase.LISTENING:
                return
            self._phase = Phase.PROCESSING
            self._barge_listening = False
            session = self._asr_sess
            self._asr_sess = None
            task = asyncio.create_task(self._finish_turn(session, time.monotonic()))
            self._turn_task = task

    async def _finish_turn(self, session: ASRSession | None, t0: float) -> None:
        text = ""
        try:
            if session is not None:
                try:
                    text = await session.finish()
                except Exception as exc:  # noqa: BLE001
                    what = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                    self._log(f"ASR 收尾失败: {what}")
                finally:
                    await self._close_session(session)

            self._log(f"[时延] ASR 收尾 {time.monotonic() - t0:.2f}s（松手后）")
            if text:
                await self._send_text_frame(M.transcript("user", text))
                self._log(f"用户: {text}")

            if not transcript_filter.has_meaningful_speech(text):
                await self._send_text_frame(M.round_skip("未识别到有效文字（可能是噪声）"))
                async with self._lock:
                    if self._phase == Phase.PROCESSING:
                        self._phase = Phase.IDLE
                return

            await self._reply(text, t0)
        except asyncio.CancelledError:
            raise
        finally:
            if self._turn_task is asyncio.current_task():
                self._turn_task = None

    async def _reply(self, text: str, t0: float) -> None:
        t_first_token: float | None = None
        t_first_audio: float | None = None
        self._turn_sequence += 1
        turn_id = self._turn_sequence
        async with self._lock:
            if self._phase != Phase.PROCESSING:
                return
            self._phase = Phase.SPEAKING
            self._active_tts_turn_id = turn_id
            self._generation_complete = False
            self._barge_detector.reset()

        await self._send_text_frame(M.tts_start(turn_id))
        buf = ""
        full = ""
        try:
            async for delta in self._llm.reply_stream(
                self.role.instructions, self._history, text
            ):
                if t_first_token is None:
                    t_first_token = time.monotonic()
                    self._log(f"[时延] LLM 首 token {t_first_token - t0:.2f}s（松手后）")
                full += delta
                buf += delta
                while True:
                    sentence, buf = _take_sentence(buf)
                    if sentence is None:
                        break
                    if sentence.strip():
                        t_first_audio = await self._speak_text(
                            sentence, t0, t_first_audio
                        )

            if buf.strip():
                t_first_audio = await self._speak_text(buf, t0, t_first_audio)

            if full.strip():
                clean = full.strip()
                await self._send_text_frame(M.transcript("assistant", clean))
                self._log(f"助手: {clean}")
                # Commit after playback_complete. A response can be fully
                # generated yet still interrupted while queued on the device.
                self._pending_history = (turn_id, text, clean)

            await self._send_text_frame(M.tts_end(turn_id))
            self._generation_complete = True
            self._log(f"[时延] 总耗时 {time.monotonic() - t0:.2f}s")
            if self._barge_config.enabled:
                self._log("TTS 下发完成，等待设备 playback_complete → IDLE")
            else:
                self._commit_pending_history(turn_id)
                async with self._lock:
                    if self._phase == Phase.SPEAKING and self._active_tts_turn_id == turn_id:
                        self._phase = Phase.IDLE
                        self._active_tts_turn_id = 0
                self._log("本轮回复完成 → IDLE")
        except asyncio.CancelledError:
            self._log(f"回答 turn={turn_id} 已被打断")
            raise
        except Exception as exc:  # noqa: BLE001
            what = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            self._log(f"处理异常: {what}")
            log.exception("处理失败")
            try:
                await self._send_text_frame(M.tts_end(turn_id))
                await self._send_text_frame(M.error(f"处理失败: {what}"))
            except Exception:  # noqa: BLE001
                pass
            async with self._lock:
                if self._phase == Phase.SPEAKING and self._active_tts_turn_id == turn_id:
                    self._phase = Phase.IDLE
                    self._active_tts_turn_id = 0
                    self._generation_complete = False
                    self._pending_history = None

    async def _speak_text(
        self, text: str, t0: float, first_audio: float | None
    ) -> float | None:
        # A long unpunctuated LLM fragment may be flushed just before its
        # trailing punctuation arrives. Do not submit that punctuation as a
        # standalone synthesis request: Doubao rejects it with error 3011.
        if not _has_speakable_text(text):
            log.debug("跳过不可合成的纯符号片段: %r", text)
            return first_audio
        async for pcm in self._tts.synthesize(text, self.role.speaker):
            if first_audio is None:
                first_audio = time.monotonic()
                self._log(f"[时延] 首音（松手→出声）{first_audio - t0:.2f}s")
            self._barge_detector.remember_playback(pcm)
            await self._send_bytes_frame(pcm)
        return first_audio

    async def on_barge_candidate(self, turn_id: int = 0) -> None:
        if (
            not self._barge_config.enabled
            or self._phase != Phase.SPEAKING
            or not self._matches_turn(turn_id, self._active_tts_turn_id)
        ):
            self._log(
                f"忽略过期或不可用的设备打断候选（turn={turn_id}, "
                f"current={self._active_tts_turn_id}, phase={self._phase.value}）"
            )
            return
        await self._begin_barge_in(
            self._barge_detector.snapshot_pre_roll(), "设备高置信候选"
        )

    async def _begin_barge_in(self, pre_roll: bytes, source: str) -> None:
        if not self._barge_config.enabled or self._phase != Phase.SPEAKING:
            return
        interrupted_turn = self._active_tts_turn_id
        task = self._turn_task
        async with self._lock:
            if self._phase != Phase.SPEAKING:
                return
            self._phase = Phase.LISTENING
            self._interrupted_tts_turn_id = interrupted_turn
            self._active_tts_turn_id = 0
            self._generation_complete = False
            self._pending_history = None
            self._barge_listening = True
            self._awaiting_barge_ack = True
            self._barge_started_at = time.monotonic()
            self._pending_barge_pre_roll = bytes(pre_roll)
            self._pending_barge_ack_audio.clear()
            self._barge_detector.reset()

        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        await self._send_text_frame(M.barge_in(interrupted_turn))
        self._log(f"已触发全双工打断（{source}）→ LISTENING，turn={interrupted_turn}")
        if task is not None and task is not asyncio.current_task() and not task.done():
            try:
                await task
            except asyncio.CancelledError:
                pass
        if not await self._start_asr():
            self._reset_barge_handshake()

    async def on_barge_ack(self, turn_id: int = 0) -> None:
        if (
            self._phase == Phase.LISTENING
            and self._barge_listening
            and self._awaiting_barge_ack
            and self._matches_turn(turn_id, self._interrupted_tts_turn_id)
        ):
            await self._complete_barge_handshake("barge_ack")

    async def _complete_barge_handshake(self, acknowledgement: str) -> None:
        if not self._awaiting_barge_ack:
            return
        elapsed_ms = int((time.monotonic() - self._barge_started_at) * 1000)
        retained = self._pending_barge_pre_roll + bytes(self._pending_barge_ack_audio)
        self._reset_barge_handshake()
        session = self._asr_sess
        if elapsed_ms <= _MAX_SAFE_BARGE_ACK_MS and retained and session is not None:
            try:
                await session.feed(retained)
                self._log(
                    f"打断确认 {acknowledgement} 延迟={elapsed_ms}ms，"
                    f"保留 {len(retained)} bytes 音频"
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("ASR pre-roll feed 失败: %s", exc)
        elif retained:
            self._log(
                f"打断确认 {acknowledgement} 延迟={elapsed_ms}ms，"
                "丢弃确认前音频以避免旧 TTS 进入转写"
            )
        self._interrupted_tts_turn_id = 0

    def _reset_barge_handshake(self) -> None:
        self._awaiting_barge_ack = False
        self._barge_started_at = 0.0
        self._pending_barge_pre_roll = b""
        self._pending_barge_ack_audio.clear()

    async def on_playback_progress(self, turn_id: int, samples: int) -> None:
        if (
            self._phase == Phase.SPEAKING
            and samples >= 0
            and self._matches_turn(turn_id, self._active_tts_turn_id)
        ):
            self._barge_detector.update_playback_cursor(samples)

    async def on_playback_complete(self, turn_id: int = 0) -> None:
        async with self._lock:
            if (
                self._phase != Phase.SPEAKING
                or not self._generation_complete
                or not self._matches_turn(turn_id, self._active_tts_turn_id)
            ):
                return
            self._phase = Phase.IDLE
            self._commit_pending_history(self._active_tts_turn_id)
            self._active_tts_turn_id = 0
            self._generation_complete = False
            self._pending_history = None
            self._barge_detector.reset()
        self._log(f"设备播放完成 → IDLE（turn={turn_id}）")

    async def on_cancel(self) -> None:
        if self._phase == Phase.LISTENING and self._barge_listening and self._awaiting_barge_ack:
            await self._complete_barge_handshake("cancel")
            return
        await self._stop_all("取消，回到 IDLE")

    async def close(self) -> None:
        await self._stop_all(None)

    async def _stop_all(self, message: str | None) -> None:
        task = self._turn_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        session = self._asr_sess
        self._asr_sess = None
        await self._close_session(session)
        async with self._lock:
            self._phase = Phase.IDLE
            self._active_tts_turn_id = 0
            self._interrupted_tts_turn_id = 0
            self._generation_complete = False
            self._pending_history = None
            self._barge_listening = False
            self._reset_barge_handshake()
            self._barge_detector.reset()
        if message:
            self._log(message)

    async def _send_text_frame(self, text: str) -> None:
        async with self._send_lock:
            await self._send_text(text)

    async def _send_bytes_frame(self, pcm: bytes) -> None:
        async with self._send_lock:
            await self._send_bytes(pcm)

    @staticmethod
    async def _close_session(session: ASRSession | None) -> None:
        if session is None:
            return
        try:
            await session.close()
        except (Exception, asyncio.CancelledError):  # noqa: BLE001
            pass

    @staticmethod
    def _matches_turn(received: int, current: int) -> bool:
        return received == 0 or received == current

    def _commit_pending_history(self, turn_id: int) -> None:
        pending = self._pending_history
        if pending is None or pending[0] != turn_id:
            return
        _, user_text, assistant_text = pending
        self._history.append({"role": "user", "content": user_text})
        self._history.append({"role": "assistant", "content": assistant_text})
        self._pending_history = None
