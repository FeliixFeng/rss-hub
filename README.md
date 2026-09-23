# rss-hub

个人 RSS 抓取服务：抓信源 → SQLite 落盘 → 只读 API 供 Ivory 轮询。  
运行在 `lunar`（VPS），systemd 常驻，端口 **8080**。

## 目录

```
/home/feng/app/rss-hub/
├── main.py            # FastAPI 路由 + X-API-Key
├── fetcher.py         # httpx 抓取 + feedparser 解析
├── store.py           # SQLite 存储
├── feeds.toml         # 信源清单
├── pyproject.toml
├── .env               # RSS_API_KEY=... （不入库）
├── data/feeds.db      # SQLite（不入库）
└── rss-hub.service
```

## 信源（feeds.toml）

| 名称 | URL | 说明 |
|------|-----|------|
| 爱范儿 | https://www.ifanr.com/feed | 科技消费 |
| 少数派 | https://sspai.com/feed | 效率工具 |
| 36氪 | https://www.36kr.com/feed | 商业科技（需 www） |
| InfoQ | https://www.infoq.cn/feed | 开发者 |
| Hacker News | https://hnrss.org/frontpage | 英文综合 |
| V2EX | https://www.v2ex.com/index.xml | 社区 |
| 量子位 | https://www.qbitai.com/feed | AI 中文 |
| 阮一峰的网络日志 | https://www.ruanyifeng.com/blog/atom.xml | 周刊/博客 |
| Solidot | https://www.solidot.org/index.rss | 奇客 |
| GitHub Blog | https://github.blog/feed/ | 开源官方 |
| GitHub Trending | https://mshibanami.github.io/GitHubTrendingRSS/daily/all.xml | 每日 trending |
| 安全内参 | https://www.secpulse.com/feed | 安全 |

已移除：机器之心官方 `/rss` 已失效（302 → HTML）。

改信源：编辑 `feeds.toml` → `POST /api/v1/sources/reload` 或 restart。

## API

- 认证：业务接口需请求头 `X-API-Key: <RSS_API_KEY>`；`/health` 免认证
- 统一响应壳：

```json
{ "ok": true, "server_time": "2026-09-23T05:00:00Z", ... }
```

失败：

```json
{ "ok": false, "server_time": "...", "error": { "code": 401, "message": "..." } }
```

### GET /health

```json
{ "ok": true, "server_time": "...", "status": "ok", "service": "rss-hub", "version": "0.3.0" }
```

### GET /api/v1/items

| 参数 | 说明 |
|------|------|
| `since` | ISO8601，只返回 `fetched_at > since`（Ivory 游标） |
| `limit` | 1–500，默认 50 |
| `source` | 精确匹配信源名 |

```json
{
  "ok": true,
  "server_time": "...",
  "count": 2,
  "items": [
    {
      "id": "4a8254eebf113598",
      "source": "V2EX",
      "title": "...",
      "url": "https://...",
      "summary": "正文截断 ≤500 字（可能含 HTML 标签）",
      "published_at": "2026-09-23T04:19:30Z",
      "fetched_at": "2026-09-23T04:56:13Z"
    }
  ]
}
```

- `id` = `sha1(url)` 前 16 hex，全局幂等  
- 时间字段：`fetched_at` 为服务端 UTC（`YYYY-MM-DDTHH:MM:SSZ`）；`published_at` 保留源格式（不统一解析）

### GET /api/v1/sources

配置 + 每源最近一次抓取结果：

```json
{
  "ok": true,
  "server_time": "...",
  "count": 12,
  "sources": [
    {
      "name": "爱范儿",
      "url": "https://www.ifanr.com/feed",
      "enabled": true,
      "last_fetched_at": "...",
      "last_ok": true,
      "last_item_count": 10,
      "last_error": null
    }
  ]
}
```

### GET /api/v1/status

```json
{
  "ok": true,
  "server_time": "...",
  "service": "rss-hub",
  "version": "0.3.0",
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

### POST /api/v1/refresh

立即抓一轮：

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
    "items_seen": 120,
    "new_items": 3,
    "purged_items": 0,
    "failures": []
  }
}
```

### POST /api/v1/sources/reload

重读 `feeds.toml`：

```json
{ "ok": true, "server_time": "...", "reloaded": 12, "enabled": 12, "names": ["..."] }
```

## 存储（SQLite: data/feeds.db）

```sql
feed_items (
  id           TEXT PRIMARY KEY,   -- sha1(url)[:16]
  source       TEXT NOT NULL,
  title        TEXT NOT NULL,
  url          TEXT NOT NULL,
  summary      TEXT NOT NULL,      -- ≤500 chars
  published_at TEXT,               -- 源格式，可空
  fetched_at   TEXT NOT NULL       -- UTC ISO8601，since 游标列
)
fetch_log (
  id, source, fetched_at, ok, item_count, error
)
meta (key, value)                  -- last_round_at / last_round_reason / migrated_at
```

- 索引：`feed_items(fetched_at)`, `feed_items(source)`, `fetch_log(source, fetched_at)`
- 保留：30 天，每轮抓取末尾 `DELETE WHERE fetched_at < now-30d`
- 去重：`INSERT OR IGNORE` on `id`

## 抓取行为

- 触发：启动即抓 + 每 60s×`RSS_POLL_INTERVAL`（默认 3600s）+ 手动 refresh
- UA：Chrome 桌面 UA（量子位等站需要）
- 单源失败只记 `fetch_log`，不中断本轮

## 运维

```bash
ssh lunar
systemctl status rss-hub
journalctl -u rss-hub -f

# 改信源后
curl -X POST -H "X-API-Key: $KEY" http://127.0.0.1:8080/api/v1/sources/reload

# 升级代码
cd /home/feng/app/rss-hub && uv sync && sudo systemctl restart rss-hub
```

Key：`/home/feng/app/rss-hub/.env` 中 `RSS_API_KEY`（禁止提交到 git）。

## 客户端（Ivory）约定

1. 本地存 `last_fetched_at`（ISO8601 UTC）
2. `GET /api/v1/items?since=<cursor>&limit=200`
3. 用响应里的 `server_time`（或本机 UTC）作为下次 `since`
4. 按 `id` 入库幂等
