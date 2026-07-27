from __future__ import annotations

import math
import struct
import unittest

from barge_in import BargeInConfig, BargeInDetector


FRAME_MS = 40


def constant_frame(amplitude: int, duration_ms: int = FRAME_MS, rate: int = 16_000) -> bytes:
    return struct.pack("<h", amplitude) * (rate * duration_ms // 1000)


def sine_frame(
    amplitude: int, frequency: int, duration_ms: int = FRAME_MS, rate: int = 16_000
) -> bytes:
    samples = [
        round(amplitude * math.sin(2 * math.pi * frequency * i / rate))
        for i in range(rate * duration_ms // 1000)
    ]
    return struct.pack(f"<{len(samples)}h", *samples)


def mixed_frame(
    amplitude1: int, frequency1: int, amplitude2: int, frequency2: int
) -> bytes:
    samples = [
        round(
            amplitude1 * math.sin(2 * math.pi * frequency1 * i / 16_000)
            + amplitude2 * math.sin(2 * math.pi * frequency2 * i / 16_000)
        )
        for i in range(16_000 * FRAME_MS // 1000)
    ]
    return struct.pack(f"<{len(samples)}h", *samples)


class BargeInDetectorTest(unittest.TestCase):
    def test_device_vad_gate_blocks_echo_until_near_end_vad_is_active(self) -> None:
        detector = BargeInDetector(
            BargeInConfig(
                enabled=True,
                rms_threshold=100,
                hold_ms=80,
                startup_guard_ms=0,
                warmup_ms=0,
            )
        )
        speech = sine_frame(2000, 700, duration_ms=40)

        self.assertFalse(detector.accept(speech, trigger_enabled=False).triggered)
        self.assertFalse(detector.accept(speech, trigger_enabled=False).triggered)
        self.assertFalse(detector.accept(speech, trigger_enabled=True).triggered)
        self.assertTrue(detector.accept(speech, trigger_enabled=True).triggered)

    def detector(self, threshold: int = 1800, hold_ms: int = 120) -> BargeInDetector:
        return BargeInDetector(
            BargeInConfig(
                enabled=True,
                rms_threshold=threshold,
                hold_ms=hold_ms,
                pre_roll_ms=300,
                startup_guard_ms=0,
                warmup_ms=0,
            )
        )

    def test_startup_guard_uses_confirmed_playback_progress(self) -> None:
        detector = BargeInDetector(
            BargeInConfig(
                enabled=True,
                rms_threshold=1800,
                hold_ms=80,
                pre_roll_ms=300,
                startup_guard_ms=600,
                warmup_ms=0,
            )
        )
        detector.remember_playback(
            sine_frame(4200, 440, duration_ms=1000, rate=24_000)
        )
        loud = sine_frame(4000, 900)

        detection = detector.accept(loud)
        self.assertTrue(detection.startup_guard)
        self.assertFalse(detection.triggered)

        detector.update_playback_cursor(16_000 * 500 // 1000)
        detection = detector.accept(loud)
        self.assertTrue(detection.startup_guard)
        self.assertFalse(detection.triggered)

        detector.update_playback_cursor(16_000 * 600 // 1000)
        self.assertFalse(detector.accept(loud).triggered)
        self.assertTrue(detector.accept(loud).triggered)

    def test_native_16k_playback_reference_rejects_echo(self) -> None:
        detector = self.detector(threshold=500, hold_ms=80)
        playback = sine_frame(4200, 440, duration_ms=1000, rate=16_000)
        detector.remember_playback(playback, sample_rate=16_000)
        detector.update_playback_cursor(16_000)

        detection = detector.accept(sine_frame(4200, 440))

        self.assertTrue(detection.playback_echo)
        self.assertFalse(detection.triggered)

    def test_warmup_uses_stricter_threshold_and_hold(self) -> None:
        detector = BargeInDetector(
            BargeInConfig(
                enabled=True,
                rms_threshold=1800,
                hold_ms=80,
                pre_roll_ms=300,
                startup_guard_ms=0,
                warmup_ms=2500,
                warmup_rms_threshold=3200,
                warmup_hold_ms=160,
            )
        )
        detector.remember_playback(
            sine_frame(4200, 440, duration_ms=3000, rate=24_000)
        )
        detector.update_playback_cursor(16_000)

        for _ in range(4):
            detection = detector.accept(constant_frame(3000))
            self.assertFalse(detection.triggered)
            self.assertTrue(detection.warmup)
            self.assertEqual(3200, detection.effective_threshold)
            self.assertEqual(160, detection.required_hold_ms)

        for _ in range(3):
            self.assertFalse(detector.accept(constant_frame(4000)).triggered)
        self.assertTrue(detector.accept(constant_frame(4000)).triggered)

    def test_sustained_speech_triggers_after_hold(self) -> None:
        detector = self.detector()
        self.assertFalse(detector.accept(constant_frame(4000)).triggered)
        self.assertFalse(detector.accept(constant_frame(4000)).triggered)
        detection = detector.accept(constant_frame(4000))
        self.assertTrue(detection.triggered)
        self.assertEqual(4000, detection.rms)
        self.assertEqual(3 * 16_000 * 2 * FRAME_MS // 1000, len(detection.captured_audio))

    def test_quiet_frame_resets_hold(self) -> None:
        detector = self.detector()
        detector.accept(constant_frame(4000))
        detector.accept(constant_frame(4000))
        detector.accept(constant_frame(500))
        self.assertFalse(detector.accept(constant_frame(4000)).triggered)
        self.assertFalse(detector.accept(constant_frame(4000)).triggered)
        self.assertTrue(detector.accept(constant_frame(4000)).triggered)

    def test_correlated_playback_echo_does_not_trigger(self) -> None:
        detector = self.detector(threshold=1300, hold_ms=80)
        detector.remember_playback(sine_frame(4200, 440, duration_ms=1000, rate=24_000))
        for _ in range(6):
            detection = detector.accept(sine_frame(2600, 440))
            self.assertFalse(detection.triggered)
            self.assertTrue(detection.playback_echo)

    def test_unrelated_near_end_speech_triggers_with_reference(self) -> None:
        detector = self.detector(threshold=1300, hold_ms=80)
        detector.remember_playback(sine_frame(4200, 440, duration_ms=1000, rate=24_000))
        self.assertFalse(detector.accept(sine_frame(2600, 900)).triggered)
        self.assertTrue(detector.accept(sine_frame(2600, 900)).triggered)

    def test_double_talk_survives_echo_rejection(self) -> None:
        detector = self.detector(threshold=600, hold_ms=80)
        detector.remember_playback(sine_frame(4200, 440, duration_ms=1000, rate=24_000))
        frame = mixed_frame(2200, 440, 1800, 900)
        self.assertFalse(detector.accept(frame).triggered)
        detection = detector.accept(frame)
        self.assertGreater(detection.correlation, 0.5)
        self.assertTrue(detection.triggered)


if __name__ == "__main__":
    unittest.main()
