"""入口：uvicorn 启动 bin 网关。

用法（在 bin/ 目录下）：
    python main.py
或：
    uvicorn app:app --host 0.0.0.0 --port 8765
"""
from __future__ import annotations

import uvicorn

from config import settings


def main() -> None:
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
