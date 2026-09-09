import asyncio
import logging

from fastapi import FastAPI

from database import ensure_indexes
from routes.celo import router as celo_router
from routes.partner_auth import router as partner_auth_router
from workers.celo_deposit_watcher import celo_deposit_watcher_loop

logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="Celo BaaS Gateway",
    description="Reusable, multi-tenant Celo deposits/swaps/withdrawals gateway for partner platforms.",
)

app.include_router(partner_auth_router)
app.include_router(celo_router)

_watcher_stop_event: asyncio.Event | None = None
_watcher_task: asyncio.Task | None = None


@app.on_event("startup")
async def on_startup():
    global _watcher_stop_event, _watcher_task
    await ensure_indexes()
    _watcher_stop_event = asyncio.Event()
    _watcher_task = asyncio.create_task(celo_deposit_watcher_loop(_watcher_stop_event))


@app.on_event("shutdown")
async def on_shutdown():
    if _watcher_stop_event:
        _watcher_stop_event.set()
    if _watcher_task:
        await _watcher_task


@app.get("/health")
async def health():
    return {"status": "ok", "service": "celo-baas-gateway"}
