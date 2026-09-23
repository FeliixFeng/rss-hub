# rss-hub

**Small, boring, reliable RSS infrastructure.**  
Crawl feeds on a schedule → store deduped items in SQLite → expose a versioned, key-authenticated API for downstream pollers.

```
feeds.toml ──► fetcher (httpx + feedparser) ──► SQLite ──► /api/v1/* ──► your reader / hub
                     ▲                              │
                     └── boot + interval + manual ──┘
```

Designed as the **fetch half** of a personal knowledge hub: this service never talks to your UI. A separate app pulls `since=<cursor>` twice a day (or whenever), runs its own summarization/filtering, and renders the reading experience.

## Why this shape

| Decision | Rationale |
|----------|-----------|
| Pull-only API, no webhooks | Consumer-side polling is enough for daily digests; avoids exposing inbound ports on the home server |
| Cursor = `fetched_at` (`?since=`) | Client owns its high-water mark — the hub stays stateless w.r.t. consumers and can be wiped/rebuilt |
| `id = sha1(url)[:16]` | Idempotent ingest on both sides; re-crawls are free |
| TOML feed list, not DB admin UI | Adding a source is a one-line edit + `POST /sources/reload` |
| SQLite + single process | One VPS, one folder, one systemd unit — trivial to migrate |
| `X-API-Key` on `/api/v1/*` | Public VPS port should not be an open crawl proxy |

## Quick start

```bash
# deps: Python ≥3.10, uv (or pip install fastapi uvicorn feedparser httpx tomli)
export RSS_API_KEY=$(openssl rand -hex 16)
echo "RSS_API_KEY=$RSS_API_KEY" > .env

uv sync
uv run uvicorn main:app --host 0.0.0.0 --port 8080
```

```bash
curl -s localhost:8080/health
curl -s -H "X-API-Key: $RSS_API_KEY" localhost:8080/api/v1/items?limit=3
```

Production: ship `rss-hub.service`, point `EnvironmentFile` at your `.env`, `systemctl enable --now rss-hub`.

## Project layout

```
├── main.py           # FastAPI routes, auth, envelope, background loop
├── fetcher.py        # concurrent crawl + parse + persist
├── store.py          # SQLite schema, upsert, queries, retention
├── feeds.toml        # source list (name / url / enabled)
├── rss-hub.service   # systemd unit template
├── pyproject.toml
└── data/feeds.db     # runtime (gitignored)
```

## Sources

Curated in [`feeds.toml`](feeds.toml) — tech news, AI, security, community:

| Source | Focus |
|--------|--------|
| 爱范儿 · 少数派 · 36氪 | Consumer tech / productivity / business |
| InfoQ · GitHub Blog · GitHub Trending | Engineering & open source |
| Hacker News · V2EX · Solidot | Community |
| 量子位 · 阮一峰的网络日志 | AI CN · weekly web |
| 安全内参 | Security |

Edit the file, then:

```bash
curl -X POST -H "X-API-Key: $RSS_API_KEY" localhost:8080/api/v1/sources/reload
```

> Feeds die. The service treats HTML error pages and empty feeds as first-class failures (logged per source, visible in `/api/v1/status`) instead of silently dropping them.

## API

**Auth:** `X-API-Key: <RSS_API_KEY>` on everything under `/api/v1/`.  
`/health` is open for uptime checks.

**Envelope** (success and error):

```json
{ "ok": true,  "server_time": "2026-09-23T05:00:00Z", ... }
{ "ok": false, "server_time": "...", "error": { "code": 401, "message": "..." } }
```

### Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness |
| GET | `/api/v1/items` | Incremental item pull |
| GET | `/api/v1/sources` | Config + last crawl result per source |
| GET | `/api/v1/status` | Service + DB health |
| POST | `/api/v1/refresh` | Crawl now |
| POST | `/api/v1/sources/reload` | Re-read `feeds.toml` |

### `GET /api/v1/items`

| Query | Description |
|-------|-------------|
| `since` | ISO-8601 UTC; only rows with `fetched_at > since` |
| `limit` | 1–500 (default 50) |
| `source` | Exact source name |

```json
{
  "ok": true,
  "server_time": "2026-09-23T05:05:51Z",
  "count": 2,
  "items": [
    {
      "id": "4a8254eebf113598",
      "source": "V2EX",
      "title": "...",
      "url": "https://...",
      "summary": "full article plain text (page extract preferred), ≥500 chars; items without full text are not stored",
      "published_at": "2026-09-23T04:19:30Z",
      "fetched_at": "2026-09-23T04:56:13Z"
    }
  ]
}
```

- **`id`**: `sha1(url)` truncated to 16 hex chars — stable across crawls  
- **`fetched_at`**: server UTC, the cursor column  
- **`published_at`**: left in the feed’s own format (not normalized)

### Poller contract

1. Persist `last_fetched_at` (UTC ISO-8601) on the client  
2. `GET /api/v1/items?since=$last_fetched_at&limit=200`  
3. Next cursor = response `server_time` (or your clock after a successful call)  
4. Upsert by `id` — duplicates are expected and harmless  

### `GET /api/v1/status` (shape)

```json
{
  "ok": true,
  "server_time": "...",
  "service": "rss-hub",
  "version": "0.4.0",
  "poll_interval_seconds": 3600,
  "sources_configured": 12,
  "sources_enabled": 12,
  "last_round": { "fetched_at": "...", "reason": "boot|interval|manual" },
  "db": {
    "item_count": 200,
    "oldest_item_at": "...",
    "newest_item_at": "...",
    "last_fetch_at": "...",
    "failed_fetches_24h": 0
  }
}
```

### `POST /api/v1/refresh` (shape)

```json
{
  "ok": true,
  "server_time": "...",
  "round": {
    "fetched_at": "...",
    "reason": "manual",
    "sources_total": 12,
    "sources_ok": 12,
    "sources_failed": 0,
    "items_seen": 180,
    "new_items": 3,
    "purged_items": 0,
    "failures": []
  }
}
```

## Storage

SQLite file (`data/feeds.db`), three tables:

```sql
feed_items (
  id           TEXT PRIMARY KEY,  -- sha1(url)[:16]
  source       TEXT NOT NULL,
  title        TEXT NOT NULL,
  url          TEXT NOT NULL,
  summary      TEXT NOT NULL,     -- full article plain text ≥500 chars
  published_at TEXT,              -- source format, nullable
  fetched_at   TEXT NOT NULL      -- UTC ISO-8601 — since cursor
);
-- indexes: (fetched_at), (source)

fetch_log (id, source, fetched_at, ok, item_count, error);
meta      (key, value);           -- last_round_at, last_round_reason, ...
```

- **Dedup:** primary key on `id`; re-crawls refresh `title`/`summary` without moving `fetched_at`
- **Retention:** 30 days, purged at the end of each crawl round  
- **Failure isolation:** one dead feed never aborts the round; it only writes `fetch_log`

## Crawl behavior

| Trigger | When |
|---------|------|
| Boot | Immediately on process start |
| Interval | Every `RSS_POLL_INTERVAL` seconds (default `3600`) |
| Manual | `POST /api/v1/refresh` |

Desktop Chrome `User-Agent` (several CN sites reject default library UAs). Per-source timeout 15s, max 20 entries ingested per feed per round.

**Full text:** after each feed, the hub GETs each entry `url` and extracts article text with `trafilatura` (page concurrency 12). An item is **kept only if** the final plain text is ≥ `MIN_FULL_TEXT` (500) chars; otherwise it is skipped and any existing short row is deleted. Feed body is kept when the page fetch fails but the feed already has ≥500 chars. Downstream must not re-crawl pages — the hub is the aggregation layer.

## Configuration

| Env | Default | Meaning |
|-----|---------|---------|
| `RSS_API_KEY` | — | Required; all `/api/v1/*` calls |
| `RSS_POLL_INTERVAL` | `3600` | Background crawl period (seconds) |

Secrets live in `.env` (`EnvironmentFile` for systemd) — **never commit `.env` or `data/`**.

## License

MIT
