"""Feed fetching: load feeds.toml, pull with httpx, parse via feedparser, write store."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import feedparser
import httpx

from store import item_id, log_fetch, purge_old, utcnow, upsert_items

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

FETCH_TIMEOUT = 15.0
ENTRIES_PER_FEED = 20
SUMMARY_LIMIT = 500


def load_sources(feeds_path: Path) -> list[dict[str, Any]]:
    data = tomllib.loads(feeds_path.read_text(encoding="utf-8"))
    sources = []
    for raw in data.get("source", []):
        name = (raw.get("name") or "").strip()
        url = (raw.get("url") or "").strip()
        if not name or not url:
            continue
        sources.append(
            {
                "name": name,
                "url": url,
                "enabled": bool(raw.get("enabled", True)),
            }
        )
    return sources


def _entry_published(entry: Any) -> str | None:
    for key in ("published", "updated"):
        val = getattr(entry, key, None)
        if val:
            return str(val)[:40]
    return None


async def fetch_one(
    client: httpx.AsyncClient, source: dict[str, Any]
) -> tuple[list[dict[str, Any]], str]:
    """Fetch one source. Returns (items, error). error=='' means ok."""
    name = source["name"]
    try:
        resp = await client.get(source["url"])
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        body_head = resp.content[:500].lstrip().lower()
        if b"<rss" not in body_head and b"<feed" not in body_head and b"<?xml" not in body_head:
            if body_head.startswith(b"<!doctype") or body_head.startswith(b"<html"):
                raise ValueError(
                    f"not a feed (got {ctype or 'unknown'}, html page) — url may be dead"
                )
        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            raise ValueError(f"parse error: {getattr(parsed, 'bozo_exception', 'unknown')}")
        if not parsed.entries:
            raise ValueError("feed parsed but contains 0 entries")
        now = utcnow()
        items = []
        for e in parsed.entries[:ENTRIES_PER_FEED]:
            url = str(getattr(e, "link", "") or "").strip()
            if not url:
                continue
            title = str(getattr(e, "title", "") or "").strip()[:300]
            summary = str(getattr(e, "summary", "") or "")[:SUMMARY_LIMIT]
            items.append(
                {
                    "id": item_id(url),
                    "source": name,
                    "title": title,
                    "url": url,
                    "summary": summary,
                    "published_at": _entry_published(e),
                    "fetched_at": now,
                }
            )
        return items, ""
    except Exception as ex:  # noqa: BLE001 — one bad feed must not kill the round
        return [], str(ex)


async def fetch_all(sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Fetch all enabled sources concurrently; persist results. Returns round summary."""
    enabled = [s for s in sources if s["enabled"]]
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0 Safari/537.36"
        ),
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    }
    async with httpx.AsyncClient(
        headers=headers, timeout=FETCH_TIMEOUT, follow_redirects=True
    ) as client:
        results = await asyncio.gather(*(fetch_one(client, s) for s in enabled))

    total_new = 0
    items_seen = 0
    failures: list[dict[str, str]] = []
    for source, (items, error) in zip(enabled, results):
        if error:
            log_fetch(source["name"], ok=False, item_count=0, error=error)
            failures.append({"source": source["name"], "error": error})
            continue
        items_seen += len(items)
        new = upsert_items(items)
        total_new += new
        log_fetch(source["name"], ok=True, item_count=len(items), error="")

    purged = purge_old()
    summary = {
        "fetched_at": utcnow(),
        "sources_total": len(enabled),
        "sources_ok": len(enabled) - len(failures),
        "sources_failed": len(failures),
        "items_seen": items_seen,
        "new_items": total_new,
        "purged_items": purged,
        "failures": failures,
    }
    return summary


def fetch_all_sync(sources: list[dict[str, Any]]) -> dict[str, Any]:
    return asyncio.run(fetch_all(sources))
