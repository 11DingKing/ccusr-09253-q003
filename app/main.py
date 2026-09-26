"""服务端业务模块。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import services
from .db import SessionLocal
from .routers import router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # 重启恢复：把停机期间已到生效时刻的定时规则物化为 active。
    # 规则激活本由 effective_from 派生，这里只是补登状态与审计。
    try:
        db = SessionLocal()
        try:
            services.recover_scheduled_rules(db)
        finally:
            db.close()
    except Exception:
        # 数据库尚未建表（首次启动）时不阻塞服务拉起。
        pass
    yield


app = FastAPI(
    title="Practice Hours Guard",
    version="0.2.0",
    description=(
        "Event-sourced practice-hours compliance service. Check-ins, mentor "
        "confirmations and leave corrections are append-only; compliance is "
        "derived by replay and can be frozen into an immutable snapshot. "
        "Hour-caliber rules are versioned with effective intervals and pinned "
        "to events at import time."
    ),
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
