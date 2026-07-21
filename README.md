# bin — 实时语音对话网关

景区智能垃圾桶语音方案的新一代后端网关。**替代 `ljt`**（Java + 阿里云百炼 Omni 端到端），
改用 **Python + 豆包（火山引擎）分体**（ASR → LLM → TTS 独立），音色可定制。

```
ESP32 / 浏览器  ──WebSocket──►  bin 网关  ──►  豆包 ASR ──► 豆包 LLM ──► 豆包 TTS
                  (1:1 复刻 ljt 协议)                                  (音色可复刻/设计)
```

## 为什么换

| | ljt（旧） | bin（新） |
|---|---|---|
| 引擎 | 百炼 Omni Realtime（端到端） | 豆包分体 ASR/LLM/TTS |
| 音色 | 只能从预设列表选 | 录音复刻 / 音色设计 / 预置，可任意定制 |
| 语言 | Java | Python（参考 pipecat 的 pipeline 编排） |
| 调试 | 必须上硬件 | 自带浏览器测试页 |

## 架构

```
bin/
├── main.py / app.py        # FastAPI：设备 WS(@/) + 测试网页(GET /) 同端口
├── gateway.py              # 设备 WebSocket（复刻 ljt 协议 + device_id 踢线）
├── conversation.py         # 引擎：状态机 IDLE/LISTENING/PROCESSING/SPEAKING + 编排
├── messages.py             # 设备 JSON 协议（与 ESP32 固件 1:1）
├── frames.py               # 轻量帧/事件（致敬 pipecat）
├── transcript_filter.py    # 有效话术判定
├── config.py               # 环境变量配置
├── roles.json              # 角色（人设 + 豆包 speaker 音色）
├── roles/                  # 角色注册表
├── services/               # base(ABC) + doubao_asr / doubao_llm / doubao_tts + _volc 编解码
└── web/index.html          # 浏览器测试页（16k 采集 / 24k 播放）
```

编排思路参考 [pipecat-ai/pipecat](https://github.com/pipecat-ai/pipecat)：用帧/事件驱动状态机，
但务实——不造通用图引擎。服务层用抽象基类（`ASRService`/`LLMService`/`TTSService`）隔离，
换引擎（如 TTS 升级 V3、换别家 ASR）不改引擎与协议层。

## 快速开始

### 1. 安装依赖

```bash
cd bin
pip install -e .          # 或 pip install fastapi "uvicorn[standard]" websockets openai python-dotenv
```

### 2. 配置

```bash
cp .env.example .env
# 编辑 .env，填入 VOLC_APPID / VOLC_ACCESS_TOKEN / ARK_API_KEY
```

豆包凭证获取：
- **语音（ASR/TTS）**：[火山引擎语音控制台](https://console.volcengine.com/speech/service/10028) → 获取 APP ID 与 Access Token
- **LLM**：[火山方舟控制台](https://console.volcengine.com/ark) → API Key 管理 → 创建 API Key

### 3. 启动

```bash
python main.py
# 或 uvicorn app:app --host 0.0.0.0 --port 8765
```

成功输出：

```
bin 网关已就绪 端口=8765 默认角色=trash_can barge_in=False
可用角色: trash_can, fanhang_assistant, shanshan, ancient_sage, ocean_poet, nature_sprite
连接示例: ws://<host>:8765?device_id=bin-001&role_id=shanshan
```

## 浏览器测试页

启动后浏览器打开 `http://<host>:8765/`：

1. 填设备 ID、角色 ID，点「连接」
2. 收到 `就绪` 后，「按住说话，松开发送」
3. 说完松开 → 自动 ASR → LLM → TTS 播放
4. 日志区显示转写、回复、跳过、错误

> 先在网页测通语音链路，再上 ESP32 硬件。

## ESP32 接入

固件无需改动，直接连（协议与 ljt 完全一致，详见 `ljt/docs/设备WebSocket接口.md`）：

```
ws://<host>:8765?device_id=bin-001&role_id=shanshan
```

- 上行：Text JSON（`listen_start`/`listen_end`/`cancel`/`set_role`/`heartbeat`）+ Binary PCM **16k/16bit/mono**
- 下行：Text JSON（`ready`/`tts_start`/`tts_end`/`transcript`/`round_skip`/`error`）+ Binary PCM **24k/16bit/mono**
- 相同 `device_id` 踢线：旧连接收 `error` + 关闭码 `4001`

## 音色定制（换方案的核心）

`roles.json` 每个角色的 `speaker` 字段即豆包 `voice_type`，三种来源：

1. **预置音色**：如 `BV700_streaming`（灿灿·女）、`BV701_streaming`（擎苍·男）。免费音色需在控制台下 0 元单。
   完整列表见火山引擎「小模型音色列表」。
2. **声音复刻**：[声音复刻 API V3](https://www.volcengine.com/docs/6561/2227958)，传 10~30s 录音训练，
   得到 `S_xxxxx` 作为 `voice_type`。
3. **音色设计**：[音色设计 HTTP](https://www.volcengine.com/docs/6561/2277844)（`…/api/v3/tts/voice_design`），
   用自然语言描述（年龄/性别/声线/情感）或上传图片生成全新音色。

> 复刻 2.0 / 音色设计 / expressive 需要 **TTS V3 双向流式**。当前 MVP 用 V1 `ws_binary`（支持预置 + 复刻 1.0 `S_` 音色），
> V3 实现已用 `TTSService` 接口隔离，后续接入不改引擎（见路线图）。

## 配置项

见 `.env.example`。常用：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `8765` | 服务端口（设备 WS + 网页共用） |
| `DEFAULT_ROLE_ID` | `trash_can` | 默认角色 |
| `VOLC_APPID` / `VOLC_ACCESS_TOKEN` | — | 豆包语音凭证 |
| `ASR_RESOURCE_ID` | `volc.bigasr.sauc.duration` | ASR 资源 ID（2.0 用 `volc.seedasr.sauc.duration`） |
| `TTS_CLUSTER` | `volcano_tts` | TTS V1 集群 |
| `TTS_VOICE_TYPE` | `BV700_streaming` | 兜底音色（角色未指定时） |
| `ARK_API_KEY` | — | 方舟 LLM Key |
| `ARK_MODEL` | `doubao-seed-1-6-flash-250828` | 豆包 Model ID 或 `ep-xxx` |
| `MIN_TRANSCRIPT_CHARS` | `1` | 有效话术最小字数 |
| `BARGE_IN_ENABLED` | `false` | 全双工打断（MVP 未启用） |

## 服务实现（事实来自官方文档）

| 服务 | 接口 | 端点 | 鉴权 |
|---|---|---|---|
| ASR | 大模型流式 V3 | `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel` | HTTP 头 `X-Api-App-Key/Access-Key/Resource-Id` |
| LLM | 方舟 Ark（OpenAI 兼容） | `https://ark.cn-beijing.volces.com/api/v3` | `Authorization: Bearer <ARK_API_KEY>` |
| TTS | V1 ws_binary | `wss://openspeech.bytedance.com/api/v1/tts/ws_binary` | `Authorization: Bearer; {token}` + body `app{appid,token,cluster}` |

ASR/TTS 的二进制帧编解码见 `services/_volc.py`（4 字节头 + 序列号 + gzip 负载，大端）。

## 路线图

- [x] 设备协议（复刻 ljt，固件零改动）
- [x] 豆包 ASR / LLM / TTS 分体
- [x] 浏览器测试页
- [x] 半双工对话流程
- [ ] TTS 升级 V3 双向流式（复刻 2.0 / 音色设计 / expressive / LLM 流式→TTS 流式低延迟）
- [ ] 全双工 barge-in（服务端 VAD + `barge_in`，参考 `frames.InterruptionFrame` 与 ljt `BargeInDetector`）
- [ ] CI/CD：git push 自动部署到服务器

## 说明

- 豆包无官方 Python SDK 包裹 ASR/TTS 的 WebSocket 二进制协议，`services/_volc.py` 是按官方文档自实现的编解码，**需实机验证**（尤其 V1 TTS 响应帧）。
- 联调时关注服务端日志 `X-Tt-Logid`，便于火山引擎侧排错。
