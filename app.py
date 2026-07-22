"""FastAPI 应用：设备 WebSocket（路径 /）+ 测试网页（GET /）同一端口。

ESP32 固件连 ws://host:8765?device_id=xxx&role_id=xxx（与 ljt 一致）；
浏览器打开 http://host:8765/ 即得测试页。WebSocket 升级请求与 GET 由
Starlette 路由器按协议类型分别路由到同一 "/"，互不冲突。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse

import gateway
from config import settings
from roles import registry as role_registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("bin")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    problems = gateway.init_services()
    for p in problems:
        log.warning("配置告警: %s", p)
    log.info(
        "bin 网关已就绪 端口=%d 默认角色=%s barge_in=%s",
        settings.port, settings.default_role_id, settings.barge_in_enabled,
    )
    if settings.barge_in_enabled:
        log.info(
            "全双工打断: RMS=%d 持续=%dms 预录=%dms 起播保护=%dms",
            settings.barge_in_rms_threshold,
            settings.barge_in_hold_ms,
            settings.barge_in_pre_roll_ms,
            settings.barge_in_startup_guard_ms,
        )
    log.info("可用角色: %s", ", ".join(r.id for r in role_registry.all()))
    log.info("连接示例: ws://<host>:%d?device_id=bin-001&role_id=shanshan", settings.port)
    yield


app = FastAPI(title="bin voice gateway", lifespan=lifespan)


@app.get("/")
async def index() -> HTMLResponse:
    html_path = Path(__file__).parent / "web" / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.websocket("/")
async def device_ws(ws: WebSocket) -> None:
    await ws.accept()
    client_host = ws.client.host if ws.client else "unknown"
    device_id = ws.query_params.get("device_id") or f"web-{client_host}"
    role_id = ws.query_params.get("role_id")
    await gateway.handle_device(ws, device_id, role_id)
