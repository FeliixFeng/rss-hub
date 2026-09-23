# rss-hub

**Small, boring, reliable RSS infrastructure.**  
Crawl a tight source list on a schedule → extract full article text → store deduped rows in SQLite → expose a versioned, key-authenticated pull API for one downstream app.

```
feeds.toml ──► feed fetch ──► page full-text (trafilatura) ──► SQLite ──► /api/v1/* ──► consumer
                    ▲                                              │
                    └── boot + every 1h + manual refresh ──────────┘
```

**Phase 1 status: frozen at `0.4.1`.**  
This repo is the **aggregation layer only** (crawl + full text + buffer). Classification, AI digests, and UI live downstream and **must not re-crawl article URLs**.

## Why this shape

| Decision | Rationale |
|----------|-----------|
| Pull-only API, no webhooks | Consumer polls 1–2×/day; hub never pushes |
| Cursor = `fetched_at` (`?since=`) | Consumer owns the high-water mark; hub stays rebuildable |
| `id = sha1(url)[:16]` | Same article never duplicates; re-crawls are free |
| Keep only full text (≥500 chars) | Every stored row is readable end-to-end; no lead-only noise |
| 30-day rolling buffer | Hub is a hand-off pool, not an archive — consumer stores what it keeps |
| 6 sources + `domain` tags | Volume fits a daily digest; domains ready for consumer grouping |
| `X-API-Key` on `/api/v1/*` | Public VPS port must not be an open crawl proxy |

## Quick start

```bash
# deps: Python ≥3.10, uv
export RSS_API_KEY=$(openssl rand -hex 16)
echo "RSS_API_KEY=$RSS_API_KEY" > .env

uv sync
uv run uvicorn main:app --host 0.0.0.0 --port 8080
```

```bash
curl -s localhost:8080/health
curl -s -H "X-API-Key: $RSS_API_KEY" 'localhost:8080/api/v1/items?limit=3'
```

Production: `rss-hub.service` + `EnvironmentFile=.env` + `systemctl enable --now rss-hub`.

## Project layout

```
├── main.py           # FastAPI routes, auth, envelope, background loop
├── fetcher.py        # feed fetch + page full-text + persist
├── store.py          # SQLite schema, upsert, queries, retention
├── feeds.toml        # sources: name / url / enabled / domain
├── rss-hub.service   # systemd unit template
├── pyproject.toml
└── data/feeds.db     # runtime (gitignored)
```

## Sources (frozen set)

Configured in [`feeds.toml`](feeds.toml). **Enabled (6)** — one `domain` each:

| Source | domain | Role |
|--------|--------|------|
| 量子位 | `ai` | Chinese AI news |
| Hacker News | `community` | English tech front page |
| InfoQ | `dev` | Engineering depth |
| 少数派 | `product` | Tools / productivity |
| 阮一峰的网络日志 | `dev` | Weekly web digest |
| GitHub Blog | `dev` | Official long-form |

**Disabled (kept in file, easy to re-enable):** 爱范儿, 36氪, Solidot, V2EX, GitHub Trending, 安全内参.

`domain` enum: `ai` | `dev` | `security` | `product` | `community`.

```bash
# after editing feeds.toml
curl -X POST -H "X-API-Key: $RSS_API_KEY" localhost:8080/api/v1/sources/reload
```

### Volume (expectation)

| Metric | Value |
|--------|--------|
| Pool after a full re-seed (all sources’ recent entries) | ~**67** rows (2026-09-23 baseline; exact mix varies) |
| Steady-state **new** articles / day | **Not fixed yet** — measure 2–3 calendar days of true `id` increments (estimate ~20–40/day) |
| Hub retention | **30 days** rolling |

Stock ≠ daily intake. The consumer should treat “how many rows came back with `since=`” as the ground truth for intake, not the pool size.

## API

**Auth:** `X-API-Key: <RSS_API_KEY>` on everything under `/api/v1/`.  
`/health` is open for uptime checks.

**Envelope:**

```json
{ "ok": true,  "server_time": "2026-09-23T05:00:00Z", ... }
{ "ok": false, "server_time": "...", "error": { "code": 401, "message": "..." } }
```

### Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness |
| GET | `/api/v1/items` | Incremental full-text pull |
| GET | `/api/v1/sources` | Config (+ `domain`) + last crawl result |
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
  "server_time": "2026-09-23T06:26:58Z",
  "count": 1,
  "items": [
    {
      "id": "4a8254eebf113598",
      "source": "量子位",
      "title": "...",
      "url": "https://...",
      "summary": "full article plain text, page extract preferred, always ≥500 chars when stored",
      "published_at": "...",
      "fetched_at": "..."
    }
  ]
}
```

- **`id`**: `sha1(url)[:16]` — primary key both sides  
- **`fetched_at`**: cursor column; does **not** move when the same URL is re-crawled with unchanged content  
- **`summary`**: readable article body only (HTML stripped). Rows shorter than `MIN_FULL_TEXT` (500) are never kept  

### Poller contract (consumer — frozen for Phase 2)

1. Persist `last_fetched_at` (UTC ISO-8601). Empty = first sync: call without `since` and page until `count == 0` (or accept one page of 500).  
2. `GET /api/v1/items?since=$last_fetched_at&limit=500` with `X-API-Key`.  
3. Upsert every row by **`id`** (idempotent).  
4. Set `last_fetched_at = response.server_time` only after a successful parse.  
5. On failure, leave the cursor unchanged and retry next cycle.  
6. Recommended cadence: **1–2 times per day** (hub already crawls hourly).  
7. **Do not fetch `url` for article bodies** — `summary` is the full text hub guarantees.  
8. Hub buffer = **30 days**. Anything the consumer wants long-term must be stored downstream (recommended: short rolling inbox + optional keep-list).  
9. Domain grouping: use `domain` from `GET /api/v1/sources` (not hard-coded in the consumer).

### `GET /api/v1/status` (shape)

```json
{
  "ok": true,
  "server_time": "...",
  "service": "rss-hub",
  "version": "0.4.1",
  "poll_interval_seconds": 3600,
  "sources_configured": 12,
  "sources_enabled": 6,
  "last_round": { "fetched_at": "...", "reason": "boot|interval|manual" },
  "db": {
    "item_count": 67,
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
    "sources_total": 6,
    "sources_ok": 6,
    "sources_failed": 0,
    "items_seen": 73,
    "items_kept": 67,
    "items_dropped": 6,
    "new_items": 0,
    "purged_items": 0,
    "failures": []
  }
}
```

`items_seen` → hydrated candidates; `items_dropped` → dropped for &lt;500 chars; `new_items` → rows actually inserted/updated with a changed summary this round.

## Storage

SQLite `data/feeds.db`:

```sql
feed_items (
  id           TEXT PRIMARY KEY,  -- sha1(url)[:16]
  source       TEXT NOT NULL,
  title        TEXT NOT NULL,
  url          TEXT NOT NULL,
  summary      TEXT NOT NULL,     -- full article plain text ≥500 chars
  published_at TEXT,              -- source format, nullable
  fetched_at   TEXT NOT NULL      -- UTC ISO8601 — since cursor
);
-- indexes: (fetched_at), (source)

fetch_log (id, source, fetched_at, ok, item_count, error);
meta      (key, value);           -- last_round_at, last_round_reason, ...
```

| Rule | Behavior |
|------|----------|
| Dedup | PK `id`; conflict updates `title`/`summary` only |
| Retention | **30 days** at end of each round; also purge any row still &lt;500 chars |
| Failure isolation | One dead feed only writes `fetch_log` |

## Crawl behavior

| Trigger | When |
|---------|------|
| Boot | Immediately on process start |
| Interval | Every `RSS_POLL_INTERVAL` seconds (default **3600**) |
| Manual | `POST /api/v1/refresh` |

- Chrome desktop UA (feed + page). Feed timeout 15s; ≤20 entries per feed per round.  
- **Full text:** GET each entry URL, extract with `trafilatura` (page concurrency 12). Keep only if plain text ≥ 500 chars; else drop (and delete a short existing row). If the page fails but the feed body is already ≥500 chars, keep the feed body.

## Configuration

| Env | Default | Meaning |
|-----|---------|---------|
| `RSS_API_KEY` | — | Required for `/api/v1/*` |
| `RSS_POLL_INTERVAL` | `3600` | Background crawl period (seconds) |

Secrets stay in `.env` (systemd `EnvironmentFile`) — **never commit `.env` or `data/`**.

## Phase boundary

| In this repo (done) | Downstream (later) |
|---------------------|--------------------|
| Fetch, full text, dedup, 30-day buffer, pull API | Pull 1–2×/day, store, AI filter, daily digest, UI |
| Source list + domains | Preference learning, rankings, archive policy |

No webhooks, no classification models, no article-page re-crawl by the consumer.

## License

MIT
