# Recipes — worked TQL examples

Copy-paste starting points for the questions that don't fit `dau` / `mau` / `groupby` / `events`. All use the auto-filter sentinel — pipe through `tdq.py query -`.

## DAU (raw, for when you want to customize)

```json
{
  "queryType":"timeseries",
  "granularity":"day",
  "aggregations":[{"type":"userCount","name":"dau"}],
  "filter":{"__auto_app_and_test_mode_filter__":true},
  "relativeIntervals":[{"beginningDate":{"component":"day","offset":-30,"position":"beginning"},
                         "endDate":{"component":"day","offset":0,"position":"end"}}]
}
```

## Events per user per day

```json
{
  "queryType":"timeseries",
  "granularity":"day",
  "aggregations":[
    {"type":"eventCount","name":"events"},
    {"type":"userCount","name":"users"}
  ],
  "postAggregations":[
    {"type":"arithmetic","name":"events_per_user","fn":"/",
     "fields":[{"type":"fieldAccess","fieldName":"events"},{"type":"fieldAccess","fieldName":"users"}]}
  ],
  "filter":{"__auto_app_and_test_mode_filter__":true},
  "relativeIntervals":[{"beginningDate":{"component":"day","offset":-30,"position":"beginning"},
                         "endDate":{"component":"day","offset":0,"position":"end"}}]
}
```

## License split, conditional aggregations in one pass

```json
{
  "queryType":"timeseries",
  "granularity":"day",
  "aggregations":[
    {"type":"filtered","filter":{"type":"selector","dimension":"license","value":"Pro"},
     "aggregator":{"type":"userCount","name":"pro_users"}},
    {"type":"filtered","filter":{"type":"selector","dimension":"license","value":"Free"},
     "aggregator":{"type":"userCount","name":"free_users"}}
  ],
  "filter":{"__auto_app_and_test_mode_filter__":true},
  "relativeIntervals":[{"beginningDate":{"component":"day","offset":-30,"position":"beginning"},
                         "endDate":{"component":"day","offset":0,"position":"end"}}]
}
```

## Top-N countries by users

```json
{
  "queryType":"topN",
  "granularity":"all",
  "dimension":{"type":"default","dimension":"countryCode","outputName":"country"},
  "metric":{"type":"numeric","metric":"users"},
  "threshold":20,
  "aggregations":[{"type":"userCount","name":"users"}],
  "filter":{"__auto_app_and_test_mode_filter__":true},
  "relativeIntervals":[{"beginningDate":{"component":"day","offset":-30,"position":"beginning"},
                         "endDate":{"component":"day","offset":0,"position":"end"}}]
}
```

## Multi-dim groupBy: license × OS

```json
{
  "queryType":"groupBy",
  "granularity":"all",
  "dimensions":[
    {"type":"default","dimension":"license","outputName":"license"},
    {"type":"default","dimension":"majorSystemVersion","outputName":"os"}
  ],
  "aggregations":[
    {"type":"eventCount","name":"events"},
    {"type":"userCount","name":"users"}
  ],
  "filter":{"type":"and","fields":[
    {"type":"selector","dimension":"appID","value":"<APP-UUID>"},
    {"type":"selector","dimension":"isTestMode","value":"false"},
    {"type":"selector","dimension":"type","value":"App_launched"}
  ]},
  "relativeIntervals":[{"beginningDate":{"component":"day","offset":-30,"position":"beginning"},
                         "endDate":{"component":"day","offset":0,"position":"end"}}]
}
```

Note: auto-filter sentinel doesn't mix with custom filters — inline the full `and`.

## Major-version distribution (extraction)

```json
{
  "queryType":"topN",
  "granularity":"all",
  "dimension":{"type":"extraction","dimension":"appVersion","outputName":"major",
               "extractionFn":{"type":"regex","expr":"^(\\d+)","index":1,"replaceMissingValueWith":"unknown"}},
  "metric":{"type":"numeric","metric":"users"},
  "threshold":10,
  "aggregations":[{"type":"userCount","name":"users"}],
  "filter":{"__auto_app_and_test_mode_filter__":true},
  "relativeIntervals":[{"beginningDate":{"component":"day","offset":-7,"position":"beginning"},
                         "endDate":{"component":"day","offset":0,"position":"end"}}]
}
```

## Funnel: launch → first transcription

```json
{
  "queryType":"funnel",
  "granularity":"all",
  "steps":[
    {"type":"selector","dimension":"type","value":"App_launched"},
    {"type":"selector","dimension":"type","value":"Transcription_started"},
    {"type":"selector","dimension":"type","value":"Transcription_completed"}
  ],
  "filter":{"__auto_app_and_test_mode_filter__":true},
  "relativeIntervals":[{"beginningDate":{"component":"day","offset":-30,"position":"beginning"},
                         "endDate":{"component":"day","offset":0,"position":"end"}}]
}
```

## Weekly retention, 8 weeks

```json
{
  "queryType":"retention",
  "granularity":"all",
  "targetEvent":"App_launched",
  "retentionPeriods":8,
  "dateRange":{"component":"week","count":8},
  "filter":{"__auto_app_and_test_mode_filter__":true}
}
```

## Cohort: App Store users only, weekly retention

```json
{
  "queryType":"retention",
  "granularity":"all",
  "targetEvent":"App_launched",
  "retentionPeriods":8,
  "dateRange":{"component":"week","count":8},
  "filter":{"type":"and","fields":[
    {"type":"selector","dimension":"appID","value":"<APP-UUID>"},
    {"type":"selector","dimension":"isTestMode","value":"false"},
    {"type":"selector","dimension":"isAppStore","value":"true"}
  ]}
}
```

## Fixed-date range (Q1 2026)

Swap `relativeIntervals` for `intervals`:

```json
"intervals":["2026-01-01T00:00:00Z/2026-03-31T23:59:59Z"]
```

## Raw scan — last 50 events for one user

```json
{
  "queryType":"scan",
  "granularity":"all",
  "columns":["receivedAt","type","appVersion","license"],
  "resultFormat":"compactedList",
  "limit":50,
  "filter":{"type":"and","fields":[
    {"type":"selector","dimension":"appID","value":"<APP-UUID>"},
    {"type":"selector","dimension":"isTestMode","value":"false"},
    {"type":"selector","dimension":"clientUser","value":"<hashed-id>"}
  ]},
  "relativeIntervals":[{"beginningDate":{"component":"day","offset":-7,"position":"beginning"},
                         "endDate":{"component":"day","offset":0,"position":"end"}}]
}
```
