# Query types

TQL has 7 `queryType` values. Pick one, fill the skeleton, ship.

| Type | Use when | Required extras |
|---|---|---|
| `timeseries` | Metric(s) over time buckets | `aggregations`, `granularity` |
| `topN` | Rank one dimension by one metric | `dimension`, `metric`, `threshold`, `aggregations` |
| `groupBy` | Break down by 1+ dimensions, no implicit ranking | `dimensions` (array), `aggregations` |
| `scan` | Raw row dump, no aggregation | `columns`, `resultFormat` |
| `funnel` | Ordered-step drop-off | `steps` (array of filters) |
| `retention` | Cohort return rates | `dateRange`, `retentionPeriods`, `targetEvent` |
| `experiment` | A/B test analysis | Rare. See docs if asked. |

## Universal fields

Every query has these:

```json
{
  "queryType": "<type>",
  "granularity": "day",
  "filter": { "__auto_app_and_test_mode_filter__": true },
  "relativeIntervals": [{
    "beginningDate": {"component":"day","offset":-30,"position":"beginning"},
    "endDate":       {"component":"day","offset":0,"position":"end"}
  }]
}
```

## `timeseries` skeleton

```json
{
  "queryType": "timeseries",
  "granularity": "day",
  "aggregations": [
    {"type":"eventCount","name":"events"},
    {"type":"userCount","name":"users"}
  ],
  "filter": { "__auto_app_and_test_mode_filter__": true },
  "relativeIntervals": [{"beginningDate":{"component":"day","offset":-30,"position":"beginning"},
                          "endDate":{"component":"day","offset":0,"position":"end"}}]
}
```

One row per bucket. Bucket size = `granularity`.

## `topN` skeleton

```json
{
  "queryType": "topN",
  "granularity": "all",
  "dimension": {"type":"default","dimension":"countryCode","outputName":"country"},
  "metric": {"type":"numeric","metric":"count"},
  "threshold": 20,
  "aggregations": [{"type":"eventCount","name":"count"}],
  "filter": { "__auto_app_and_test_mode_filter__": true },
  "relativeIntervals": [{"beginningDate":{"component":"day","offset":-30,"position":"beginning"},
                          "endDate":{"component":"day","offset":0,"position":"end"}}]
}
```

One dimension only. Ranked. Capped at `threshold`.

## `groupBy` skeleton

```json
{
  "queryType": "groupBy",
  "granularity": "all",
  "dimensions": [
    {"type":"default","dimension":"license","outputName":"license"},
    {"type":"default","dimension":"majorSystemVersion","outputName":"os"}
  ],
  "aggregations": [{"type":"eventCount","name":"count"}],
  "filter": { "__auto_app_and_test_mode_filter__": true },
  "relativeIntervals": [{"beginningDate":{"component":"day","offset":-30,"position":"beginning"},
                          "endDate":{"component":"day","offset":0,"position":"end"}}]
}
```

Multiple dimensions OK. No implicit ordering — sort client-side.

## `scan` skeleton

```json
{
  "queryType": "scan",
  "granularity": "all",
  "columns": ["receivedAt","type","clientUser","appVersion"],
  "resultFormat": "compactedList",
  "limit": 100,
  "filter": { "__auto_app_and_test_mode_filter__": true },
  "relativeIntervals": [{"beginningDate":{"component":"day","offset":-1,"position":"beginning"},
                          "endDate":{"component":"day","offset":0,"position":"end"}}]
}
```

Raw event dump. Use sparingly — big payloads.

## `funnel` skeleton

See `funnel.md` for full semantics.

```json
{
  "queryType": "funnel",
  "granularity": "all",
  "steps": [
    {"type":"selector","dimension":"type","value":"App_launched"},
    {"type":"selector","dimension":"type","value":"Transcription_started"},
    {"type":"selector","dimension":"type","value":"Transcription_completed"}
  ],
  "filter": { "__auto_app_and_test_mode_filter__": true },
  "relativeIntervals": [{"beginningDate":{"component":"day","offset":-30,"position":"beginning"},
                          "endDate":{"component":"day","offset":0,"position":"end"}}]
}
```

## `retention` skeleton

See `retention.md`.

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

## `experiment`

Rare. Specialized A/B surface. If user asks, re-fetch `https://telemetrydeck.com/docs/tql/query/` for current shape.
