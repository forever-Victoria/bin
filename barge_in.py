"""Server-side full-duplex barge-in detection.

The detector consumes 16 kHz mono PCM from the microphone and keeps a short
pre-roll.  Downlink TTS is retained as a 16 kHz reference so correlated
playback echo can be rejected before the RMS/hold-time decision is made.

This intentionally stays dependency-free.  The ESP32 already performs the
primary AEC; the correlation stage here is a guard against residual echo, not
a replacement for hardware AEC.
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
import math
import operator
import sys


SAMPLE_RATE = 16_000
BYTES_PER_SAMPLE = 2
REFERENCE_HISTORY_SAMPLES = SAMPLE_RATE * 5
CORRELATION_SAMPLE_STRIDE = 4
CORRELATION_DELAY_STRIDE = 40


@dataclass(frozen=True)
class BargeInConfig:
    enabled: bool = False
    rms_threshold: int = 40
    hold_ms: int = 80
    pre_roll_ms: int = 300
    echo_correlation_threshold: float = 0.62
    echo_residual_rms: int = 40
    min_residual_ratio: float = 0.45
    reference_window_ms: int = 1500
    startup_guard_ms: int = 600
    warmup_ms: int = 2500
    warmup_rms_threshold: int = 60
    warmup_hold_ms: int = 160


@dataclass(frozen=True)
class Detection:
    triggered: bool = False
    rms: int = 0
    residual_rms: int = 0
    correlation: float = 0.0
    residual_ratio: float = 0.0
    delay_ms: int = -1
    captured_audio: bytes = b""
    playback_echo: bool = False
    startup_guard: bool = False
    warmup: bool = False
    effective_threshold: int = 0
    required_hold_ms: int = 0


@dataclass(frozen=True)
class _EchoMatch:
    correlation: float
    residual_rms: int
    delay_ms: int


class BargeInDetector:
    """Detect sustained near-end speech while retaining microphone pre-roll."""

    def __init__(self, config: BargeInConfig) -> None:
        self.config = config
        self._hold_samples = max(1, SAMPLE_RATE * config.hold_ms // 1000)
        self._startup_guard_samples = max(
            0, SAMPLE_RATE * config.startup_guard_ms // 1000
        )
        self._warmup_samples = max(0, SAMPLE_RATE * config.warmup_ms // 1000)
        self._warmup_hold_samples = max(
            1, SAMPLE_RATE * config.warmup_hold_ms // 1000
        )
        self._max_pre_roll_bytes = (
            SAMPLE_RATE * BYTES_PER_SAMPLE * config.pre_roll_ms // 1000
        )
        self._max_reference_delay = (
            SAMPLE_RATE * config.reference_window_ms // 1000
        )
        self._pre_roll = bytearray()
        self._reference: list[int] = []
        self._reference_total = 0
        self._playback_cursor: int | None = None
        self._rate_carry: list[int] = []
        self._loud_samples = 0
        self._triggered = False

    def reset(self) -> None:
        self._pre_roll.clear()
        self._reference.clear()
        self._reference_total = 0
        self._playback_cursor = None
        self._rate_carry.clear()
        self._loud_samples = 0
        self._triggered = False

    def accept(self, pcm: bytes) -> Detection:
        if self._triggered or len(pcm) < BYTES_PER_SAMPLE:
            return Detection()

        current = bytes(pcm[: len(pcm) & ~1])
        samples = _samples(current)
        rms = _rms(samples)
        echo, residual = self._find_best_echo(samples, rms)
        ratio = echo.residual_rms / max(1, rms)
        correlated = echo.correlation >= self.config.echo_correlation_threshold
        warmup = self.warmup_active()
        effective_threshold = (
            max(self.config.rms_threshold, self.config.warmup_rms_threshold)
            if warmup
            else self.config.rms_threshold
        )
        required_hold_samples = (
            self._warmup_hold_samples if warmup else self._hold_samples
        )
        required_hold_ms = (
            self.config.warmup_hold_ms if warmup else self.config.hold_ms
        )
        residual_has_speech = (
            echo.residual_rms >= effective_threshold
            and (
                not correlated
                or (
                    echo.residual_rms >= self.config.echo_residual_rms
                    and ratio >= self.config.min_residual_ratio
                )
            )
        )
        echo_only = correlated and not residual_has_speech

        # Hardware AEC and VAD can briefly report the first playback transient
        # as near-end speech. Base this guard on confirmed device playback,
        # rather than tts_start or downlink arrival, so network/TTS latency does
        # not consume it before the loudspeaker actually starts.
        startup_guard = self.startup_guard_active()
        if startup_guard:
            self._loud_samples = 0
            self._remember_pre_roll(current)
            return Detection(
                rms=rms,
                residual_rms=echo.residual_rms,
                correlation=echo.correlation,
                residual_ratio=ratio,
                delay_ms=echo.delay_ms,
                playback_echo=echo_only,
                startup_guard=True,
                warmup=warmup,
                effective_threshold=effective_threshold,
                required_hold_ms=required_hold_ms,
            )

        if echo_only:
            self._loud_samples = 0
            return Detection(
                rms=rms,
                residual_rms=echo.residual_rms,
                correlation=echo.correlation,
                residual_ratio=ratio,
                delay_ms=echo.delay_ms,
                playback_echo=True,
                warmup=warmup,
                effective_threshold=effective_threshold,
                required_hold_ms=required_hold_ms,
            )

        # Preserve the original hardware-AEC microphone audio for ASR.  The
        # fitted residual is a detector feature only and is never fed to ASR.
        self._remember_pre_roll(current)
        if residual_has_speech:
            self._loud_samples += len(samples)
        else:
            self._loud_samples = 0

        triggered = self._loud_samples >= required_hold_samples
        if triggered:
            self._triggered = True
        return Detection(
            triggered=triggered,
            rms=rms,
            residual_rms=echo.residual_rms,
            correlation=echo.correlation,
            residual_ratio=ratio,
            delay_ms=echo.delay_ms,
            captured_audio=bytes(self._pre_roll) if triggered else b"",
            warmup=warmup,
            effective_threshold=effective_threshold,
            required_hold_ms=required_hold_ms,
        )

    def snapshot_pre_roll(self) -> bytes:
        return bytes(self._pre_roll)

    def startup_guard_active(self) -> bool:
        if self._startup_guard_samples <= 0:
            return False
        return (
            self._playback_cursor is None
            or self._playback_cursor < self._startup_guard_samples
        )

    def warmup_active(self) -> bool:
        if self._warmup_samples <= 0:
            return False
        return (
            self._playback_cursor is None
            or self._playback_cursor < self._warmup_samples
        )

    def remember_playback(self, pcm: bytes, sample_rate: int = 24_000) -> None:
        """Retain the PCM actually sent to the device as a 16 kHz reference."""
        source = list(_samples(pcm[: len(pcm) & ~1]))
        if sample_rate == SAMPLE_RATE:
            converted = source
            self._rate_carry.clear()
        elif sample_rate == 24_000:
            values = self._rate_carry + source
            complete = len(values) - (len(values) % 3)
            converted = []
            for i in range(0, complete, 3):
                converted.append(values[i])
                converted.append((values[i + 1] + values[i + 2]) // 2)
            self._rate_carry = values[complete:]
        else:
            raise ValueError(f"unsupported playback reference rate: {sample_rate}")
        if converted:
            self._reference.extend(converted)
            self._reference_total += len(converted)
            overflow = len(self._reference) - REFERENCE_HISTORY_SAMPLES
            if overflow > 0:
                del self._reference[:overflow]

    def update_playback_cursor(self, samples_16k: int) -> None:
        self._playback_cursor = max(0, min(samples_16k, self._reference_total))

    def _remember_pre_roll(self, pcm: bytes) -> None:
        if self._max_pre_roll_bytes <= 0:
            return
        self._pre_roll.extend(pcm[-self._max_pre_roll_bytes :])
        overflow = len(self._pre_roll) - self._max_pre_roll_bytes
        if overflow > 0:
            del self._pre_roll[:overflow]

    def _find_best_echo(
        self, mic: array[int], mic_rms: int
    ) -> tuple[_EchoMatch, array[int]]:
        count = len(mic)
        if count < SAMPLE_RATE // 100 or len(self._reference) < count:
            return _EchoMatch(0.0, mic_rms, -1), mic

        oldest_absolute = self._reference_total - len(self._reference)
        last_start = len(self._reference) - count
        if self._playback_cursor is not None:
            cursor_relative = self._playback_cursor - oldest_absolute
            last_start = max(0, min(last_start, cursor_relative - count))
        first_start = max(0, last_start - self._max_reference_delay)

        starts = list(range(first_start, last_start + 1, CORRELATION_DELAY_STRIDE))
        if not starts or starts[-1] != last_start:
            starts.append(last_start)
        best_correlation = 0.0
        best_start = -1
        best_gain = 0.0
        best_intercept = 0.0
        mic_values = list(mic[::CORRELATION_SAMPLE_STRIDE])
        mic_sum = sum(mic_values)
        mic_sum_sq = sum(map(operator.mul, mic_values, mic_values))
        for start in starts:
            corr, gain, intercept = self._correlation_at(
                mic_values, mic_sum, mic_sum_sq, count, start
            )
            if corr > best_correlation:
                best_correlation = corr
                best_start = start
                best_gain = gain
                best_intercept = intercept

        if best_start < 0:
            return _EchoMatch(0.0, mic_rms, -1), mic

        residual = array("h")
        for i, value in enumerate(mic):
            prediction = round(best_gain * self._reference[best_start + i] + best_intercept)
            residual.append(max(-32768, min(32767, value - prediction)))
        delay_ms = (last_start - best_start) * 1000 // SAMPLE_RATE
        return (
            _EchoMatch(best_correlation, _rms(residual), delay_ms),
            residual,
        )

    def _correlation_at(
        self,
        mic_values: list[int],
        sum_mic: int,
        sum_mic_sq: int,
        mic_sample_count: int,
        start: int,
    ) -> tuple[float, float, float]:
        ref_values = self._reference[
            start : start + mic_sample_count : CORRELATION_SAMPLE_STRIDE
        ]
        count = len(mic_values)
        sum_ref = sum(ref_values)
        sum_ref_sq = sum(map(operator.mul, ref_values, ref_values))
        sum_product = sum(map(operator.mul, mic_values, ref_values))
        covariance = count * sum_product - sum_mic * sum_ref
        mic_energy = count * sum_mic_sq - sum_mic * sum_mic
        ref_energy = count * sum_ref_sq - sum_ref * sum_ref
        if mic_energy <= 0 or ref_energy <= 0:
            return 0.0, 0.0, sum_mic / max(1, count)
        correlation = abs(covariance) / math.sqrt(mic_energy * ref_energy)
        gain = covariance / ref_energy
        return correlation, gain, sum_mic / count - gain * (sum_ref / count)


def calculate_rms(pcm: bytes) -> int:
    return _rms(_samples(pcm[: len(pcm) & ~1]))


def _samples(pcm: bytes) -> array[int]:
    values: array[int] = array("h")
    values.frombytes(pcm)
    if sys.byteorder != "little":
        values.byteswap()
    return values


def _rms(samples: array[int]) -> int:
    if not samples:
        return 0
    return int(math.sqrt(sum(value * value for value in samples) / len(samples)))
