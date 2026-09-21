# Retention queries

`queryType: "retention"` computes cohort return rates: of users who first appeared in week N, what % came back in week N+1, N+2, ...

## Shape

```json
{
  "queryType": "retention",
  "granularity": "all",
  "targetEvent": "App_launched",
  "retentionPeriods": 8,
  "dateRange": {"component":"week","count":8},
  "filter": { "__auto_app_and_test_mode_filter__": true }
}
```

- `targetEvent` — the event that marks a user as "active" in a period. Usually `App_launched` or a core engagement event.
- `retentionPeriods` — how many follow-up periods to compute (inclusive of period 0).
- `dateRange` — `{component, count}` defines both the cohort bucket size and window length.
  - `component`: `day | week | month`
  - `count`: number of cohorts to emit

## Result shape

Triangular matrix — one cohort per row, one column per retention period:

```json
[{
  "result": {
    "cohorts": [
      {"cohortDate":"2026-02-23","size":210,"retention":[1.00,0.38,0.21,0.14,0.11,0.09,0.08,0.07]},
      {"cohortDate":"2026-03-02","size":188,"retention":[1.00,0.41,0.24,0.15,0.12,0.10,0.08,null]},
      {"cohortDate":"2026-03-09","size":232,"retention":[1.00,0.39,0.22,0.16,0.13,0.11,null,null]},
      ...
    ]
  }
}]
```

`retention[0]` is always 1.00 (the cohort itself). `null` entries are periods still in the future at query time. `size` is the cohort's initial user count.

## Typical windows

- **Weekly retention, 8 weeks**: `{"component":"week","count":8}` + `retentionPeriods: 8`
- **Daily retention, 30 days**: `{"component":"day","count":30}` + `retentionPeriods: 30`
- **Monthly retention, 6 months**: `{"component":"month","count":6}` + `retentionPeriods: 6`

Match `retentionPeriods` to `dateRange.count` unless you specifically want a shorter follow-up.

## Semantics

- **Cohort definition**: users whose FIRST `targetEvent` falls in that period's window.
- **Return definition**: at least one `targetEvent` in the follow-up period.
- **Identity = `clientUser`.** Anonymous/identified-mode switches split users.
- **Filter at top level** to restrict the cohort (e.g. `isAppStore=true` for App Store users only). The `filter` applies to ALL events, both cohort-building and return-counting — so don't filter out the `targetEvent` accidentally.

## Reading the numbers

- `retention[1]` = week-1 retention (return rate in the immediate next period). Typical SaaS: 20–40%.
- Look diagonally down + right for "has retention improved?" — compare `retention[1]` across cohorts.
- A sudden drop in a column usually means the `targetEvent` stopped firing for part of the population. Cross-check with `events` subcommand.
