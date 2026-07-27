from __future__ import annotations

import asyncio
import json
import struct
import unittest

from barge_in import BargeInConfig
from conversation import Conversation, Phase, _Pcm16EdgeFader
from roles import VoiceRole
from services.base import ASRService, ASRSession, LLMService, TTSService


class FakeAsrSession(ASRSession):
    def __init__(self, text: str) -> None:
        self.text = text
        self.fed: list[bytes] = []
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def feed(self, pcm: bytes) -> None:
        self.fed.append(pcm)

    async def finish(self) -> str:
        return self.text

    async def close(self) -> None:
        pass


class FakeAsr(ASRService):
    def __init__(self) -> None:
        self.sessions: list[FakeAsrSession] = []

    def session(self) -> FakeAsrSession:
        session = FakeAsrSession("第一个问题" if not self.sessions else "打断问题")
        self.sessions.append(session)
        return session


class SlowLlm(LLMService):
    def __init__(self) -> None:
        self.cancelled = False

    async def reply_stream(self, system: str, history: list[dict], user_text: str):
        del system, history, user_text
        try:
            yield "这是一句会被打断的回答。"
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class FakeTts(TTSService):
    async def synthesize(self, text: str, speaker: str):
        del text, speaker
        # 200 ms of 24 kHz reference audio.
        yield struct.pack("<h", 800) * 4800


class FiniteLlm(LLMService):
    async def reply_stream(self, system: str, history: list[dict], user_text: str):
        del system, history, user_text
        yield "完整回答。"


class TrailingPunctuationLlm(LLMService):
    async def reply_stream(self, system: str, history: list[dict], user_text: str):
        del system, history, user_text
        # The first delta reaches the no-punctuation flush limit. The next
        # delta used to become a punctuation-only TTS request (error 3011).
        yield "垃" * 40
        yield "。"


class RejectSymbolOnlyTts(TTSService):
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def synthesize(self, text: str, speaker: str):
        del speaker
        if not any(char.isalnum() for char in text):
            raise RuntimeError("TTS 错误 3011: invalid text")
        self.texts.append(text)
        yield struct.pack("<h", 800) * 480


class ConversationBargeInTest(unittest.IsolatedAsyncioTestCase):
    def test_tts_fragment_edges_are_faded_without_changing_pcm_length(self) -> None:
        fader = _Pcm16EdgeFader(sample_rate=1000, fade_ms=5)
        first = fader.feed(struct.pack("<h", 10_000) * 12)
        last = fader.finish()
        samples = struct.unpack("<" + "h" * ((len(first) + len(last)) // 2), first + last)

        self.assertEqual(12 * 2, len(first) + len(last))
        self.assertEqual(0, samples[0])
        self.assertEqual(10_000, samples[5])
        self.assertEqual(0, samples[-1])

    def test_short_tts_fragment_fades_both_edges(self) -> None:
        fader = _Pcm16EdgeFader(sample_rate=1000, fade_ms=5)
        self.assertEqual(b"", fader.feed(struct.pack("<h", 10_000) * 3))
        samples = struct.unpack("<hhh", fader.finish())

        self.assertEqual((0, 1250, 0), samples)

    async def test_punctuation_only_fragment_is_not_sent_to_tts(self) -> None:
        sent_text: list[dict] = []

        async def send_text(payload: str) -> None:
            sent_text.append(json.loads(payload))

        tts = RejectSymbolOnlyTts()
        conv = Conversation(
            role=VoiceRole("test", "Test", "speaker", "be concise"),
            send_text=send_text,
            send_bytes=lambda _: asyncio.sleep(0),
            logger=lambda _: None,
            asr=FakeAsr(),
            llm=TrailingPunctuationLlm(),
            tts=tts,
            barge_config=BargeInConfig(enabled=False),
        )
        await conv.on_listen_start()
        await conv.on_listen_end()
        for _ in range(20):
            if conv.phase == Phase.IDLE:
                break
            await asyncio.sleep(0)

        self.assertEqual(["垃" * 40], tts.texts)
        self.assertFalse(any(item["type"] == "error" for item in sent_text))
        transcript = next(
            item for item in sent_text
            if item["type"] == "transcript" and item["role"] == "assistant"
        )
        self.assertEqual("垃" * 40 + "。", transcript["text"])
        await conv.close()

    async def test_full_duplex_turn_waits_for_real_playback_completion(self) -> None:
        sent_text: list[dict] = []

        async def send_text(payload: str) -> None:
            sent_text.append(json.loads(payload))

        conv = Conversation(
            role=VoiceRole("test", "Test", "speaker", "be concise"),
            send_text=send_text,
            send_bytes=lambda _: asyncio.sleep(0),
            logger=lambda _: None,
            asr=FakeAsr(),
            llm=FiniteLlm(),
            tts=FakeTts(),
            barge_config=BargeInConfig(enabled=True),
        )
        await conv.on_listen_start()
        await conv.on_listen_end()
        for _ in range(20):
            if any(item["type"] == "tts_end" for item in sent_text):
                break
            await asyncio.sleep(0)

        end = next(item for item in sent_text if item["type"] == "tts_end")
        self.assertEqual(Phase.SPEAKING, conv.phase)
        await conv.on_playback_complete(end["turn_id"] + 1)
        self.assertEqual(Phase.SPEAKING, conv.phase)
        await conv.on_playback_complete(end["turn_id"])
        self.assertEqual(Phase.IDLE, conv.phase)
        await conv.close()

    async def test_receive_path_stays_live_and_barge_in_starts_new_asr(self) -> None:
        sent_text: list[dict] = []
        first_audio = asyncio.Event()

        async def send_text(payload: str) -> None:
            sent_text.append(json.loads(payload))

        async def send_bytes(payload: bytes) -> None:
            self.assertTrue(payload)
            first_audio.set()

        asr = FakeAsr()
        llm = SlowLlm()
        conv = Conversation(
            role=VoiceRole("test", "Test", "speaker", "be concise"),
            send_text=send_text,
            send_bytes=send_bytes,
            logger=lambda _: None,
            asr=asr,
            llm=llm,
            tts=FakeTts(),
            barge_config=BargeInConfig(
                enabled=True,
                rms_threshold=1800,
                hold_ms=80,
                pre_roll_ms=300,
                startup_guard_ms=0,
                warmup_ms=0,
            ),
        )

        await conv.on_listen_start()
        await conv.on_audio(struct.pack("<h", 1000) * 640)
        await conv.on_listen_end()
        # on_listen_end returns while response generation continues.
        await asyncio.wait_for(first_audio.wait(), timeout=1)
        self.assertEqual(Phase.SPEAKING, conv.phase)

        loud = struct.pack("<h", 4000) * 640
        await conv.on_audio(loud)
        await conv.on_audio(loud)
        self.assertEqual(Phase.LISTENING, conv.phase)
        self.assertTrue(llm.cancelled)
        self.assertEqual(2, len(asr.sessions))

        barge = next(item for item in sent_text if item["type"] == "barge_in")
        self.assertGreater(barge["turn_id"], 0)
        self.assertFalse(any(item["type"] == "tts_end" for item in sent_text))

        await conv.on_barge_ack(barge["turn_id"])
        self.assertTrue(asr.sessions[1].fed)
        await conv.close()


if __name__ == "__main__":
    unittest.main()
