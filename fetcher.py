"""Feed fetching: load feeds.toml, pull with httpx, parse via feedparser, write store."""

from __future__ import annotations

import asyncio
import html
import re
import sys
from pathlib import Path
from typing import Any

import feedparser
import httpx
import trafilatura

from store import (
    delete_items,
    item_id,
    log_fetch,
    purge_old,
    purge_short_summaries,
    utcnow,
    upsert_items,
)

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

FETCH_TIMEOUT = 15.0
ENTRIES_PER_FEED = 20
SUMMARY_LIMIT = 50_000
MIN_FULL_TEXT = 500
PAGE_CONCURRENCY = 12

_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
_BR_RE = re.compile(r"(?i)<br\s*/?>")
_P_CLOSE_RE = re.compile(r"(?i)</p\s*>")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t ]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    text = _SCRIPT_STYLE_RE.sub(" ", raw)
    text = _BR_RE.sub("\n", text)
    text = _P_CLOSE_RE.sub("\n\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RE.sub(" ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = _MULTI_NL_RE.sub("\n\n", text)
    return text.strip()


def clip_text(text: str, limit: int = SUMMARY_LIMIT) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for stop in ("\n", "。", "！", "？", ". ", "！", "；", "; "):
        pos = cut.rfind(stop)
        if pos >= int(limit * 0.7):
            return cut[: pos + len(stop)].strip()
    return cut.rstrip()


def entry_body(entry: Any) -> str:
    # content[] is often the full article; summary/description may be a short excerpt —
    # pick the longest so we don't throw away the body when both exist.
    parts: list[str] = []
    content = getattr(entry, "content", None)
    if content:
        for block in content:
            val = getattr(block, "value", None) or (block.get("value") if isinstance(block, dict) else None)
            if val:
                parts.append(str(val))
    summary = str(getattr(entry, "summary", "") or "")
    description = str(getattr(entry, "description", "") or "")
    for cand in (summary, description):
        if cand and cand not in parts:
            parts.append(cand)
    if not parts:
        return ""
    return max(parts, key=len)


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


def extract_page_text(html: str) -> str:
    if not html:
        return ""
    try:
        text = trafilatura.extract(html) or ""
    except Exception:
        return ""
    return html_to_text(text)


async def fetch_full_text(
    client: httpx.AsyncClient, url: str, feed_text: str
) -> str:
    page_text = ""
    try:
        resp = await client.get(url)
        if resp.status_code < 400:
            page_text = extract_page_text(resp.text)
    except Exception:
        page_text = ""
    if len(page_text) >= len(feed_text):
        return page_text
    return feed_text


async def hydrate_full_texts(
    client: httpx.AsyncClient, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not items:
        return []
    sem = asyncio.Semaphore(PAGE_CONCURRENCY)

    async def one(it: dict[str, Any]) -> dict[str, Any]:
        feed_text = clip_text(it.get("summary") or "")
        async with sem:
            full = await fetch_full_text(client, it["url"], feed_text)
        it["summary"] = clip_text(full)
        return it

    return list(await asyncio.gather(*(one(it) for it in items)))


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
        candidates: list[dict[str, Any]] = []
        for e in parsed.entries[:ENTRIES_PER_FEED]:
            url = str(getattr(e, "link", "") or "").strip()
            if not url:
                continue
            title = str(getattr(e, "title", "") or "").strip()[:300]
            summary = clip_text(html_to_text(entry_body(e)))
            candidates.append(
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
        items = await hydrate_full_texts(client, candidates)
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
    items_kept = 0
    items_dropped = 0
    failures: list[dict[str, str]] = []
    for source, (items, error) in zip(enabled, results):
        if error:
            log_fetch(source["name"], ok=False, item_count=0, error=error)
            failures.append({"source": source["name"], "error": error})
            continue
        items_seen += len(items)
        drop_ids = [
            it["id"]
            for it in items
            if len(it.get("summary") or "") < MIN_FULL_TEXT
        ]
        if drop_ids:
            delete_items(drop_ids)
            items_dropped += len(drop_ids)
        kept = [
            it
            for it in items
            if len(it.get("summary") or "") >= MIN_FULL_TEXT
        ]
        items_kept += len(kept)
        new = upsert_items(kept)
        total_new += new
        log_fetch(source["name"], ok=True, item_count=len(kept), error="")

    purged = purge_old()
    purged += purge_short_summaries(MIN_FULL_TEXT)
    summary = {
        "fetched_at": utcnow(),
        "sources_total": len(enabled),
        "sources_ok": len(enabled) - len(failures),
        "sources_failed": len(failures),
        "items_seen": items_seen,
        "items_kept": items_kept,
        "items_dropped": items_dropped,
        "new_items": total_new,
        "purged_items": purged,
        "failures": failures,
    }
    return summary


def fetch_all_sync(sources: list[dict[str, Any]]) -> dict[str, Any]:
    return asyncio.run(fetch_all(sources))
