# TelemetryDeck API — HTTP & auth reference

Load only when extending `tdq.py` or debugging the raw HTTP surface. For TQL syntax (query types, filters, aggregators, intervals, etc.) see `tql/index.md`. For normal usage, the CLI handles auth, `dataSource` resolution, and filter injection.

**Status:** auth and metadata run on `/api/v3`; queries run on `/api/v4/query/tql`. The v3 async query endpoints (`calculate-async` + task polling) are gone — they now 404. Personal access tokens and the query API require a paid plan. No documented per-minute rate limit.

## Base URL & auth

- Base: `https://api.telemetrydeckapi.com`
- Header: `Authorization: Bearer <token>`

### Minting a bearer (what the CLI does under `login`)

```
POST /api/v3/users/login
Authorization: Basic base64(email:password)
→ { "value": "<bearer>", "expiresAt": "<iso>", "id": "...", "user": { "id": "..." } }
```

`value` is the bearer. `expiresAt` tells you when to re-mint. The CLI treats anything within 5 minutes of expiry as stale and re-mints automatically from the Keychain-stored password.

## Identifiers

| Identifier | Where it comes from | How it's used |
|------------|---------------------|---------------|
| `appID` (UUID) | App detail in TelemetryDeck dashboard; same UUID passed to the SDK `initialize(config: .init(appID:))` | `selector` filter dimension on every query |
| `insightID` | URL when viewing an insight | Path parameter to resolve a saved insight to TQL |
| `orgID` / `userID` | `GET /api/v3/users/info` | Not needed for queries; bearer is scoped to the user |
| `dataSource` (org namespace, e.g. `com.yourorganization`) | `namespace` field of `GET /api/v3/organizations/` | Mandatory top-level key on every v4 query |

## Running a query

One synchronous call:

```
POST /api/v4/query/tql     { ...TQL..., "dataSource": "com.yourorganization" }  → result
```

`dataSource` is required; omitting it returns `401 {"reason": "Missing key 'dataSource'"}`, and passing the org UUID or display name instead of the namespace returns `401 User can not access <value>`. The CLI resolves it once and caches it in `config.json`; `TELEMETRYDECK_DATA_SOURCE` overrides.

The server may take up to ~2 minutes on a heavy query. If one pushes that, tighten `threshold` and shrink `relativeIntervals` rather than waiting — a query that slow returns data too coarse to trust.

Two result shapes come back, and `_unwrap_envelope` normalizes both to a list of buckets:

```
[{ "timestamp": "...", "result": {...} | [...] }, ...]     # observed for timeseries and topN
{ "result": { "rows": [...], "type": "topNResult" } }      # enveloped form
```

## Running a saved insight

Two-step: resolve the insight to TQL, then execute.

```
POST /api/v3/insights/<insightID>/query/     { "relativeInterval": { ... } }
→ full TQL query JSON
→ submit to /api/v4/query/tql
```

The CLI's `insight` subcommand does both.

## Error handling

- `401 Unauthorized` — bearer expired or invalid. CLI re-mints once automatically.
- `403 Forbidden` — tier gate or scope issue.
- `4xx` with a JSON body — inspect the body, the error messages are usually specific.
- `401` with a JSON `reason` — not always auth. `Missing key 'dataSource'` and `User can not access <x>` are both `dataSource` problems, and the CLI's 401 retry will mask them as a token refresh failure.
- `404` on `/api/v3/query/...` — the removed async endpoints; use `/api/v4/query/tql`.

## Where to find TQL syntax

Don't read this file for TQL — it's just auth/HTTP. Go to `tql/index.md` and follow the routing table to the specific topic (query types, filters, aggregators, intervals, granularity, funnel, retention, recipes).
