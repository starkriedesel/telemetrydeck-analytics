# TQL progressive-disclosure index

You're writing or debugging raw TQL (not using the `dau`/`mau`/`groupby`/`events` recipes). Read only the file(s) you need — each covers one slice of the TQL surface.

## Route by question

| If you need to... | Read |
|---|---|
| Pick a query type | `query-types.md` — one-line overview + skeleton for all 7 |
| Scope a query (appID, isTestMode, event name, etc.) | `filters.md` |
| Count, sum, cardinality, user count, filtered aggregation | `aggregators.md` |
| Group by or rank by a field | `dimensions.md` |
| Express "last 30d", "this month", "previous week" | `intervals.md` |
| Choose bucket size (day / week / all / quarter / none) | `granularity.md` |
| Write a funnel (ordered steps, drop-off) | `funnel.md` |
| Write a retention query (cohort return rates) | `retention.md` |
| See worked examples of common questions | `recipes.md` |

## When to use raw TQL vs. recipes

Reach for raw TQL only if the recipes don't fit. Check `SKILL.md`'s subcommand table first — `dau`, `mau`, `groupby`, `events`, and `signals` cover the bulk of real questions. Raw TQL is for:

- Multi-dimension `groupBy` (the `groupby` recipe takes one dimension only).
- `funnel` / `retention` queries (no recipes yet).
- Post-aggregations (derived metrics).
- `filtered` aggregations (conditional counts inside a single query).
- Absolute date ranges (`relativeIntervals` covers the common case; `intervals` with ISO 8601 for fixed windows).

## Rules that apply to every query

1. **Always scope to the app** with `{"type":"selector","dimension":"appID","value":"<UUID>"}` and exclude test-mode with `{"type":"selector","dimension":"isTestMode","value":"false"}`. Combine with `{"type":"and","fields":[...]}`.
2. **Or let the CLI inject them**: set `filter` to `{"__auto_app_and_test_mode_filter__": true}` and pipe through `tdq.py query -`. Saves boilerplate and prevents accidental tenant leakage.
3. **Every query needs `queryType` and `granularity`.** Even `queryType:"all"` — the field is required.
4. **Prefer `userCount` over `cardinality`** for unique-user counting (the Druid `cardinality` aggregator is deprecated in TelemetryDeck; use `thetaSketch` or `userCount`).
5. **Time scoping**: `relativeIntervals` (array, one or more objects) for dynamic windows; `intervals` (ISO 8601 strings) for fixed dates.
6. **Leave `dataSource` out.** The query API requires it, but the CLI resolves your organization's namespace and injects it. Hand-writing it is how you get `401 User can not access <value>`; the old fixed `"telemetry-signals"` value is dead and is rewritten if it appears.
