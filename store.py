"""SQLite store: schema, upsert, since-cursor queries, fetch log, retention."""

from __future__ import annotations

import hashlib
import html as html_mod
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

RetentionDays = 30

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS feed_items (
    id           TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '',
    summary      TEXT NOT NULL DEFAULT '',
    published_at TEXT,
    fetched_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_fetched_at ON feed_items(fetched_at);
CREATE INDEX IF NOT EXISTS idx_items_source ON feed_items(source);

CREATE TABLE IF NOT EXISTS fetch_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    item_count  INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_log_source_time ON fetch_log(source, fetched_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def item_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def init(db_path: Path) -> None:
    global _conn
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(db_path), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.executescript(SCHEMA)
    _conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('migrated_at', ?)",
        (utcnow(),),
    )
    _conn.commit()


def _require() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("store.init() not called")
    return _conn


def _html_to_text(raw: str) -> str:
    if not raw or "<" not in raw:
        return (raw or "").strip()
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_mod.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def migrate_html_summaries() -> int:
    # Legacy rows stored raw HTML truncated at 500 chars; rewrite to plain text once.
    with _lock:
        conn = _require()
        rows = conn.execute(
            "SELECT id, summary FROM feed_items WHERE summary LIKE '%<%'"
        ).fetchall()
        updated = 0
        for row in rows:
            cleaned = _html_to_text(row["summary"])
            if cleaned != row["summary"]:
                conn.execute(
                    "UPDATE feed_items SET summary = ? WHERE id = ?",
                    (cleaned, row["id"]),
                )
                updated += 1
        if updated:
            conn.commit()
        return updated


def delete_items(ids: list[str]) -> int:
    if not ids:
        return 0
    with _lock:
        conn = _require()
        marks = ",".join("?" for _ in ids)
        cur = conn.execute(f"DELETE FROM feed_items WHERE id IN ({marks})", ids)
        conn.commit()
        return cur.rowcount


def purge_short_summaries(min_len: int) -> int:
    with _lock:
        conn = _require()
        cur = conn.execute(
            "DELETE FROM feed_items WHERE length(summary) < ?", (min_len,)
        )
        conn.commit()
        return cur.rowcount


def upsert_items(items: list[dict[str, Any]]) -> int:
    if not items:
        return 0
    inserted = 0
    with _lock:
        conn = _require()
        for it in items:
            cur = conn.execute(
                """INSERT INTO feed_items
                   (id, source, title, url, summary, published_at, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     title = excluded.title,
                     summary = excluded.summary
                   WHERE feed_items.summary IS NOT excluded.summary""",
                (
                    it["id"],
                    it["source"],
                    it["title"],
                    it["url"],
                    it["summary"],
                    it.get("published_at"),
                    it["fetched_at"],
                ),
            )
            if cur.rowcount:
                inserted += 1
        conn.commit()
    return inserted


def log_fetch(source: str, ok: bool, item_count: int, error: str = "") -> None:
    with _lock:
        conn = _require()
        conn.execute(
            "INSERT INTO fetch_log(source, fetched_at, ok, item_count, error) VALUES (?, ?, ?, ?, ?)",
            (source, utcnow(), 1 if ok else 0, item_count, error[:500]),
        )
        conn.commit()


def get_items(
    since: str | None = None,
    limit: int = 50,
    source: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if since:
        clauses.append("fetched_at > ?")
        params.append(since)
    if source:
        clauses.append("source = ?")
        params.append(source)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit = max(1, min(limit, 500))
    with _lock:
        conn = _require()
        rows = conn.execute(
            f"""SELECT id, source, title, url, summary, published_at, fetched_at
                FROM feed_items {where}
                ORDER BY fetched_at DESC, rowid DESC
                LIMIT ?""",
            (*params, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def sources_summary(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge configured sources with last fetch-log entry per source."""
    with _lock:
        conn = _require()
        last_rows = conn.execute(
            """SELECT source, fetched_at, ok, item_count, error
               FROM fetch_log
               WHERE id IN (SELECT MAX(id) FROM fetch_log GROUP BY source)"""
        ).fetchall()
    last = {r["source"]: dict(r) for r in last_rows}
    out = []
    for s in sources:
        entry = last.get(s["name"], {})
        out.append(
            {
                "name": s["name"],
                "url": s["url"],
                "enabled": s["enabled"],
                "last_fetched_at": entry.get("fetched_at"),
                "last_ok": bool(entry["ok"]) if entry else None,
                "last_item_count": entry.get("item_count"),
                "last_error": entry.get("error") or None,
            }
        )
    return out


def status() -> dict[str, Any]:
    with _lock:
        conn = _require()
        total = conn.execute("SELECT COUNT(*) c FROM feed_items").fetchone()["c"]
        recent_fail = conn.execute(
            """SELECT COUNT(*) c FROM fetch_log
               WHERE ok = 0 AND fetched_at > datetime('now', '-24 hours')"""
        ).fetchone()["c"]
        last_any = conn.execute(
            "SELECT MAX(fetched_at) t FROM fetch_log"
        ).fetchone()["t"]
        oldest = conn.execute(
            "SELECT MIN(fetched_at) t FROM feed_items"
        ).fetchone()["t"]
        newest = conn.execute(
            "SELECT MAX(fetched_at) t FROM feed_items"
        ).fetchone()["t"]
    return {
        "item_count": total,
        "oldest_item_at": oldest,
        "newest_item_at": newest,
        "last_fetch_at": last_any,
        "failed_fetches_24h": recent_fail,
    }


def get_last_round() -> dict[str, Any] | None:
    at = get_meta("last_round_at")
    if not at:
        return None
    return {
        "fetched_at": at,
        "reason": get_meta("last_round_reason"),
    }


def set_meta(key: str, value: str) -> None:
    with _lock:
        conn = _require()
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))
        conn.commit()


def get_meta(key: str) -> str | None:
    with _lock:
        row = _require().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def purge_old(days: int = RetentionDays) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with _lock:
        conn = _require()
        cur = conn.execute("DELETE FROM feed_items WHERE fetched_at < ?", (cutoff,))
        conn.commit()
    return cur.rowcount
