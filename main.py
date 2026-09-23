"""rss-hub API — auth + routing. Response envelope: {ok, server_time, ...}."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

import store
from fetcher import fetch_all, load_sources

BASE_DIR = Path(__file__).resolve().parent
FEEDS_PATH = BASE_DIR / "feeds.toml"
DB_PATH = BASE_DIR / "data" / "feeds.db"
POLL_INTERVAL_SECONDS = int(os.environ.get("RSS_POLL_INTERVAL", "3600"))
API_KEY = os.environ.get("RSS_API_KEY", "")
VERSION = "0.4.0"

_sources_cache: list[dict[str, Any]] = []
_fetch_lock = asyncio.Lock()


def _envelope(**payload: Any) -> dict[str, Any]:
    return {"ok": True, "server_time": store.utcnow(), **payload}


def _reload_sources() -> list[dict[str, Any]]:
    global _sources_cache
    _sources_cache = load_sources(FEEDS_PATH)
    return _sources_cache


def _require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not API_KEY:
        raise HTTPException(status_code=500, detail="RSS_API_KEY not configured")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


async def _run_round(reason: str) -> dict[str, Any]:
    async with _fetch_lock:
        sources = _sources_cache or _reload_sources()
        summary = await fetch_all(sources)
        summary["reason"] = reason
        store.set_meta("last_round_at", summary["fetched_at"])
        store.set_meta("last_round_reason", reason)
        return summary


async def _poll_loop() -> None:
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            await _run_round(reason="interval")
        except asyncio.CancelledError:
            return
        except Exception:
            pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init(DB_PATH)
    store.migrate_html_summaries()
    _reload_sources()
    boot = asyncio.create_task(_run_round(reason="boot"))
    poll = asyncio.create_task(_poll_loop())
    try:
        yield
    finally:
        poll.cancel()
        boot.cancel()
        with suppress(asyncio.CancelledError):
            await poll
        with suppress(asyncio.CancelledError):
            await boot


app = FastAPI(title="rss-hub", version=VERSION, lifespan=lifespan)


@app.exception_handler(HTTPException)
def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    try:
        server_time: str | None = store.utcnow()
    except RuntimeError:
        server_time = None
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "ok": False,
            "server_time": server_time,
            "error": {"code": exc.status_code, "message": str(exc.detail)},
        },
    )


@app.get("/health")
def health() -> dict[str, Any]:
    return _envelope(status="ok", service="rss-hub", version=VERSION)


@app.get("/api/v1/items", dependencies=[Depends(_require_api_key)])
def list_items(
    since: str | None = Query(
        default=None, description="ISO8601; return items with fetched_at > since"
    ),
    limit: int = Query(default=50, ge=1, le=500),
    source: str | None = Query(default=None, description="exact source name"),
) -> dict[str, Any]:
    items = store.get_items(since=since, limit=limit, source=source)
    return _envelope(items=items, count=len(items))


@app.get("/api/v1/sources", dependencies=[Depends(_require_api_key)])
def list_sources() -> dict[str, Any]:
    sources = _sources_cache or _reload_sources()
    rows = store.sources_summary(sources)
    return _envelope(sources=rows, count=len(rows))


@app.get("/api/v1/status", dependencies=[Depends(_require_api_key)])
def get_status() -> dict[str, Any]:
    last_round = store.get_last_round()
    return _envelope(
        service="rss-hub",
        version=VERSION,
        poll_interval_seconds=POLL_INTERVAL_SECONDS,
        sources_configured=len(_sources_cache),
        sources_enabled=sum(1 for s in _sources_cache if s["enabled"]),
        last_round=last_round,
        db=store.status(),
    )


@app.post("/api/v1/refresh", dependencies=[Depends(_require_api_key)])
async def refresh() -> dict[str, Any]:
    summary = await _run_round(reason="manual")
    return _envelope(round=summary)


@app.post("/api/v1/sources/reload", dependencies=[Depends(_require_api_key)])
def reload_sources() -> dict[str, Any]:
    sources = _reload_sources()
    return _envelope(
        reloaded=len(sources),
        enabled=sum(1 for s in sources if s["enabled"]),
        names=[s["name"] for s in sources],
    )
