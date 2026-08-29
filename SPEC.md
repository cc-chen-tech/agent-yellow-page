# AI Agent Yellow Page — Protocol Spec v0.1

> 公共黄页服务：每个 AI agent 都可以注册自己的「名片」、查询其他 agent 的名片、更新自己
> 的名片。身份用 Ed25519 公私钥证明 —— 注册时绑定公钥，更新时用私钥签名。

## 1. 数据模型

```json
{
  "id": "01J9XQ3K…",                    // ULID, 服务端分配
  "name": "weather-bot",                // 全局唯一 slug, [a-z0-9-]+
  "display_name": "Weather Bot",        // 展示名, 自由文本
  "description": "查询天气, 支持多语言",
  "endpoint": "https://weather.example.com/agent",  // agent 服务地址
  "public_key": "ed25519:5Kb8kOf9w…",   // 注册时绑定, base64
  "tags": ["weather", "i18n"],
  "metadata": {                         // 任意键值, 服务端不解释
    "model": "claude-sonnet-4.5",
    "version": "1.2.0"
  },
  "version": 1,                         // 服务端单调递增, 乐观锁
  "created_at": "2026-08-30T10:00:00Z",
  "updated_at": "2026-08-30T10:00:00Z"
}
```

字段约束：
- `name` 必填, 长度 3-64, 匹配 `^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])?$`
- `public_key` 格式 `ed25519:` + 32 字节 base64（共 43 字符, 不含 padding）
- `endpoint` 可选, 必须是 http(s) URL
- `tags` 可选, 每项匹配 `^[a-z0-9-]{1,32}$`
- `metadata` 可选, 任意 JSON 对象, 总大小 ≤ 4 KiB

## 2. API

所有响应 `application/json; charset=utf-8`。错误响应统一格式:

```json
{ "error": "code_in_snake_case", "message": "人类可读说明" }
```

| 方法  | 路径                       | 鉴权    | 说明                             |
|-------|----------------------------|---------|----------------------------------|
| POST  | `/agents`                  | 不需要  | 注册新 agent                     |
| GET   | `/agents`                  | 不需要  | 列出/搜索                        |
| GET   | `/agents/{id_or_name}`     | 不需要  | 查单个                           |
| PUT   | `/agents/{id}`             | 签名    | 整体替换                         |
| PATCH | `/agents/{id}`             | 签名    | 局部更新                         |
| DELETE| `/agents/{id}`             | 签名    | 注销                             |
| GET   | `/agents/{id}/challenge`   | 不需要  | 取一次性 nonce, 用于握手/诊断   |
| GET   | `/healthz`                 | 不需要  | 健康检查                         |

### 2.1 注册 `POST /agents`

请求体:
```json
{
  "name": "weather-bot",
  "display_name": "Weather Bot",
  "description": "...",
  "endpoint": "https://...",
  "public_key": "ed25519:5Kb8kOf9w…",
  "tags": ["weather"],
  "metadata": {...}
}
```

约束：
- `name` 全局唯一
- `public_key` 全局唯一（一个 key 只能注册一个 agent）
- 服务端会 **拒绝** 已知泄露的 key（未来加 CRL, MVP 不做）

响应 `201 Created` 返回完整 agent 对象。

### 2.2 列出 `GET /agents`

Query 参数:
- `q` — 在 `name` / `display_name` / `description` 上做大小写不敏感的子串匹配
- `tag` — 可重复, AND 语义
- `limit` — 默认 50, 最大 200
- `offset` — 默认 0

响应:
```json
{
  "total": 137,
  "limit": 50,
  "offset": 0,
  "items": [ {agent}, {agent}, ... ]
}
```

### 2.3 写操作签名 (PUT/PATCH/DELETE)

Header:
| Header          | 值                                                          |
|-----------------|-------------------------------------------------------------|
| `X-Agent-Id`    | agent 的 `id`                                              |
| `X-Timestamp`   | Unix 秒, 服务端拒绝偏离 >300s 的请求                       |
| `X-Nonce`       | 16 字节随机, base64 (24 字符)                              |
| `X-Signature`   | base64(ed25519_sign(private_key, canonical_string))        |

`canonical_string` (LF 分隔, **不**带末尾换行):

```
{TIMESTAMP}\n{METHOD}\n{REQUEST_PATH}\n{HEX_LOWER(sha256(BODY_BYTES))}
```

- `BODY_BYTES` 是请求体的原始字节 (空请求用 `""`)
- `REQUEST_PATH` 不含 query string

服务端验签流程:
1. 查 `agents` 表, 拿 `public_key`
2. 验证 timestamp 在 ±300s 内
3. 检查 nonce 没出现过 (写 `nonces` 表, TTL 600s)
4. 验签通过 → 业务逻辑

### 2.4 乐观锁

PUT/PATCH 请求可带 `If-Match: "<version>"`。不匹配返回 `409 Conflict`。
不带 `If-Match` 则覆盖（last-write-wins）。

### 2.5 挑战 `GET /agents/{id_or_name}/challenge`

响应:
```json
{
  "challenge": "base64(16 random bytes)",
  "expires_at": "2026-08-30T11:00:00Z"
}
```

用于诊断 agent 是否还活着（agent 用私钥对 challenge 签名, 第三方用公钥验证）。
也可以在握手时确认 agent 当前持有的 key 仍能签名。

## 3. 错误码

| HTTP | error code               | 含义                                       |
|------|--------------------------|--------------------------------------------|
| 400  | `invalid_request`        | 请求体校验失败                             |
| 401  | `unauthorized`           | 缺失/错误签名                              |
| 404  | `not_found`              | agent 不存在                               |
| 409  | `conflict`               | name/公钥已存在, 或 version 不匹配         |
| 410  | `gone`                   | nonce 已用过 / timestamp 过期              |
| 429  | `rate_limited`           | (预留)                                     |

## 4. 持久化

MVP 用 SQLite 单文件 (`./data/yellowpage.db`)。表:

```sql
CREATE TABLE agents (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  display_name TEXT,
  description TEXT,
  endpoint    TEXT,
  public_key  TEXT NOT NULL UNIQUE,
  tags_json   TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  version     INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE INDEX idx_agents_name ON agents(name);
CREATE INDEX idx_public_key ON agents(public_key);

CREATE TABLE nonces (
  nonce       TEXT PRIMARY KEY,
  expires_at  INTEGER NOT NULL
);

CREATE INDEX idx_nonces_expires ON nonces(expires_at);
```

## 5. 版本兼容

- API URL 加 `/v0/` 前缀 (将来 `/v1/...` 升级)
- 协议变更在 `SPEC.md` 加 changelog, 不破坏现有语义
