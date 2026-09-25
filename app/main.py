"""服务端业务模块。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import engine
from .models import Base
from .routers import router
from .scheduler import scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 轻量部署直接建表；新表（规则版本/审批/审计）随之就绪。
    Base.metadata.create_all(engine)
    scheduler.start()
    try:
        yield
    finally:
        scheduler.stop()


app = FastAPI(
    title="Practice Hours Guard",
    version="0.1.0",
    lifespan=lifespan,
    description=(
        "Event-sourced practice-hours compliance service. Check-ins, mentor "
        "confirmations and leave corrections are append-only; compliance is "
        "derived by replay under the rule version pinned at import time and "
        "can be frozen into an immutable snapshot."
    ),
)

app.include_router(router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
