"""判断 ASR 是否识别到有效用户话术（移植自 ljt TranscriptFilter.java）。"""
from __future__ import annotations

import re

from config import settings

# 去除空白与中英文标点，只看是否还有有效字符。
_PUNCT = (
    "。，、！？；：「」『』（）【】《》〈〉…—～·"
    ",.!?;:\"'()[]{}<>@#$%^&*_+-=/\\|`~"
)
_STRIP_RE = re.compile(rf"[\s{re.escape(_PUNCT)}]")


def has_meaningful_speech(transcript: str | None) -> bool:
    if transcript is None:
        return False
    trimmed = transcript.strip()
    if not trimmed:
        return False
    cleaned = _STRIP_RE.sub("", trimmed)
    return len(cleaned) >= settings.min_transcript_chars
