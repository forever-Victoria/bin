"""轻量帧/事件定义（致敬 pipecat 的 frame 模型）。

bin 的引擎内部用这些事件驱动状态机：检测到用户开口、用户说完、打断、播放结束等。
不追求 pipecat 的通用性，只为让 conversation.py 的状态转移清晰可读。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Frame:
    """所有帧的基类。"""


# ── 用户/助手说话 ───────────────────────────────────────────────────────
@dataclass
class UserStartedSpeaking(Frame):
    """VAD 检测到用户开口（全双工打断触发点）。"""


@dataclass
class UserStoppedSpeaking(Frame):
    """用户说完（端侧 listen_end）。"""


@dataclass
class BotStartedSpeaking(Frame):
    """开始下发 TTS。"""


@dataclass
class BotStoppedSpeaking(Frame):
    """本轮 TTS 下发完毕。"""


# ── 控制帧（致敬 pipecat：InterruptionFrame / CancelFrame）──────────────
@dataclass
class InterruptionFrame(Frame):
    """用户打断：取消当前 TTS/LLM，回到 LISTENING。"""


@dataclass
class CancelFrame(Frame):
    """取消本轮。"""


@dataclass
class UninterruptibleFrame:
    """Mixin：标记某段流程不可被 barge-in 打断（如复位、关键通知）。"""


@dataclass
class AudioInFrame(Frame):
    """上行一帧 PCM（设备 → 网关）。"""
    pcm: bytes


@dataclass
class AudioOutFrame(Frame):
    """下行一帧 PCM（网关 → 设备 / 浏览器）。"""
    pcm: bytes


def name_of(frame: Any) -> str:
    return type(frame).__name__
