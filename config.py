"""bin 网关配置：从环境变量 / .env 读取。

豆包（火山引擎）三件套（事实来自官方文档，2026-07）：
  - ASR：大模型流式语音识别 V3
        wss://openspeech.bytedance.com/api/v3/sauc/bigmodel
        鉴权头 X-Api-App-Key / X-Api-Access-Key / X-Api-Resource-Id
        Resource-Id：volc.bigasr.sauc.duration（1.0 小时版）
  - TTS：语音合成 V1 ws_binary（可靠、示例多；V3 双向流式留作升级）
        wss://openspeech.bytedance.com/api/v1/tts/ws_binary
        cluster volcano_tts，音色 voice_type（如 BV001_streaming 通用女声）
  - LLM：方舟 Ark（OpenAI 兼容）
        base_url https://ark.cn-beijing.volces.com/api/v3
        model doubao Model ID 或 Endpoint ID（ep-xxx）

注意：新控制台「每个服务各有一对 APP ID + Access Token」。ASR 与 TTS 凭证分开配置；
若你这两个服务共用同一对，填 VOLC_APPID/VOLC_ACCESS_TOKEN 即可，ASR_*/TTS_* 留空会自动复用。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str = "") -> str:
    v = os.getenv(key)
    return default if v is None or v.strip() == "" else v.strip()


def _bool_env(key: str, default: bool = False) -> bool:
    v = _env(key).lower()
    if v == "":
        return default
    return v in ("1", "true", "yes", "on")


def _int_env(key: str, default: int) -> int:
    v = _env(key)
    if v == "":
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _float_env(key: str, default: float) -> float:
    v = _env(key)
    if v == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # ── 服务端口：设备 WebSocket 与测试网页共用一个端口 ──────────────────────
    port: int

    # ── 角色 ───────────────────────────────────────────────────────────────
    default_role_id: str
    roles_path: str

    # ── 豆包通用凭证（ASR/TTS 共用时的兜底）─────────────────────────────────
    volc_appid: str
    volc_token: str

    # ── 豆包 ASR（大模型流式 V3）──────────────────────────────────────────
    asr_appid: str           # 流式语音识别大模型 的 APP ID（留空则复用 volc_appid）
    asr_access_token: str
    asr_resource_id: str
    asr_ws_url: str
    asr_sample_rate: int     # 上行 16k

    # ── 豆包 TTS（V1 ws_binary）───────────────────────────────────────────
    tts_appid: str           # 语音合成 的 APP ID（留空则复用 volc_appid）
    tts_access_token: str
    tts_cluster: str
    tts_ws_url: str
    tts_voice_default: str   # voice_type，角色未指定时用
    tts_sample_rate: int     # 下行 24k
    tts_speed_ratio: float
    tts_resource_id: str     # 预留：V3 双向流式用 volc.service_type.10029

    # ── 豆包 LLM（方舟 Ark，OpenAI 兼容）───────────────────────────────────
    ark_api_key: str
    ark_base_url: str
    ark_model: str
    llm_max_tokens: int
    llm_temperature: float

    # ── 行为 / 兼容 ljt ────────────────────────────────────────────────────
    barge_in_enabled: bool            # ready 里上报给设备
    barge_in_rms_threshold: int       # 近端语音最低残差 RMS
    barge_in_hold_ms: int             # 连续达到阈值多久才确认打断
    barge_in_pre_roll_ms: int         # 打断触发前保留给 ASR 的音频
    barge_in_echo_correlation: float  # 与下行 TTS 的相关性阈值
    barge_in_echo_residual_rms: int   # 双讲时允许打断的最低残差 RMS
    barge_in_min_residual_ratio: float
    barge_in_reference_window_ms: int
    barge_in_startup_guard_ms: int   # TTS 实际起播后的防误打断窗口
    barge_in_warmup_ms: int          # AEC 起播收敛期
    barge_in_warmup_rms_threshold: int
    barge_in_warmup_hold_ms: int
    min_transcript_chars: int         # 有效话术最小字数
    transcript_wait_sec: int          # ASR 转写等待超时
    tts_chunk_ms: int                 # 下行音频分片
    tts_stream_lead_ms: int           # 最多提前发送多少毫秒，限制 TCP 音频积压
    device_raw_log: bool

    @classmethod
    def from_env(cls) -> "Settings":
        volc_appid = _env("VOLC_APPID")
        volc_token = _env("VOLC_ACCESS_TOKEN")
        return cls(
            port=_int_env("PORT", 8765),

            default_role_id=_env("DEFAULT_ROLE_ID", "trash_can"),
            roles_path=_env("ROLES_PATH", "roles.json"),

            volc_appid=volc_appid,
            volc_token=volc_token,

            asr_appid=_env("ASR_APPID", volc_appid),
            asr_access_token=_env("ASR_ACCESS_TOKEN", volc_token),
            asr_resource_id=_env("ASR_RESOURCE_ID", "volc.bigasr.sauc.duration"),
            asr_ws_url=_env("ASR_WS_URL",
                            "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"),
            asr_sample_rate=_int_env("ASR_SAMPLE_RATE", 16000),

            tts_appid=_env("TTS_APPID", volc_appid),
            tts_access_token=_env("TTS_ACCESS_TOKEN", volc_token),
            tts_cluster=_env("TTS_CLUSTER", "volcano_tts"),
            tts_ws_url=_env("TTS_WS_URL",
                            "wss://openspeech.bytedance.com/api/v1/tts/ws_binary"),
            tts_voice_default=_env("TTS_VOICE_TYPE", "BV001_streaming"),
            tts_sample_rate=_int_env("TTS_SAMPLE_RATE", 24000),
            tts_speed_ratio=float(_env("TTS_SPEED_RATIO", "1.0") or "1.0"),
            tts_resource_id=_env("TTS_RESOURCE_ID", "volc.service_type.10029"),

            ark_api_key=_env("ARK_API_KEY"),
            ark_base_url=_env("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
            ark_model=_env("ARK_MODEL", "doubao-seed-1-6-flash-250828"),
            llm_max_tokens=_int_env("LLM_MAX_TOKENS", 256),
            llm_temperature=float(_env("LLM_TEMPERATURE", "0.8") or "0.8"),

            barge_in_enabled=_bool_env("BARGE_IN_ENABLED", False),
            barge_in_rms_threshold=_int_env("BARGE_IN_RMS_THRESHOLD", 25),
            barge_in_hold_ms=_int_env("BARGE_IN_HOLD_MS", 80),
            barge_in_pre_roll_ms=_int_env("BARGE_IN_PRE_ROLL_MS", 300),
            barge_in_echo_correlation=_float_env("BARGE_IN_ECHO_CORRELATION", 0.62),
            barge_in_echo_residual_rms=_int_env("BARGE_IN_ECHO_RESIDUAL_RMS", 25),
            barge_in_min_residual_ratio=_float_env("BARGE_IN_MIN_RESIDUAL_RATIO", 0.25),
            barge_in_reference_window_ms=_int_env("BARGE_IN_REFERENCE_WINDOW_MS", 1500),
            barge_in_startup_guard_ms=_int_env("BARGE_IN_STARTUP_GUARD_MS", 600),
            barge_in_warmup_ms=_int_env("BARGE_IN_WARMUP_MS", 2500),
            barge_in_warmup_rms_threshold=_int_env("BARGE_IN_WARMUP_RMS_THRESHOLD", 40),
            barge_in_warmup_hold_ms=_int_env("BARGE_IN_WARMUP_HOLD_MS", 160),
            min_transcript_chars=_int_env("MIN_TRANSCRIPT_CHARS", 1),
            transcript_wait_sec=_int_env("TRANSCRIPT_WAIT_SEC", 12),
            tts_chunk_ms=_int_env("TTS_CHUNK_MS", 40),
            tts_stream_lead_ms=_int_env("TTS_STREAM_LEAD_MS", 1000),
            device_raw_log=_bool_env("DEVICE_RAW_LOG", True),
        )

    def sanity_check(self) -> list[str]:
        """返回缺失/可疑配置的提示列表（空列表表示 OK）。"""
        problems: list[str] = []
        if not self.asr_appid or not self.asr_access_token:
            problems.append("ASR_APPID / ASR_ACCESS_TOKEN（或 VOLC_APPID/VOLC_ACCESS_TOKEN）未配置，ASR 不可用")
        if not self.tts_appid or not self.tts_access_token:
            problems.append("TTS_APPID / TTS_ACCESS_TOKEN（或 VOLC_APPID/VOLC_ACCESS_TOKEN）未配置，TTS 不可用")
        if not self.ark_api_key:
            problems.append("ARK_API_KEY 未配置，LLM 将不可用")
        return problems


settings = Settings.from_env()
