# Dimensions (DimensionSpecs)

Used in `topN` (single `dimension`) and `groupBy` (array `dimensions`). Two spec types: `default` and `extraction`.

## `default` — plain field

```json
{"type":"default","dimension":"license","outputName":"license"}
```

`dimension` = source field name. `outputName` = column name in the result. Keep them equal unless there's a reason.

Optional `outputType`: `STRING` (default), `LONG`, `FLOAT`. Cast works only if the raw value parses.

## `extraction` — transform before grouping

Apply a function to the raw dimension value, group by the transformed value.

```json
{
  "type":"extraction",
  "dimension":"appVersion",
  "outputName":"major_version",
  "extractionFn":{"type":"regex","expr":"^(\\d+)\\.","index":1}
}
```

### `extractionFn` flavors

| Type | What it does |
|---|---|
| `regex` | `expr` + `index` — extract capture group |
| `substring` | `index` + `length` — slice |
| `partial` | `expr` — keep only values matching regex, else null |
| `searchQuery` | Match a Druid search spec |
| `timeFormat` | Re-format `__time` values |
| `lookup` | Map through a lookup table (rarely available) |
| `javascript` | Likely disabled in managed deployments |
| `stringFormat` | Printf-style format |
| `upper` / `lower` | Case change |
| `cascade` | Chain multiple extractionFns |

Example — bucket appVersion into major versions only:

```json
"dimension": {
  "type":"extraction",
  "dimension":"appVersion",
  "outputName":"major",
  "extractionFn":{"type":"regex","expr":"^(\\d+)","index":1,"replaceMissingValueWith":"unknown"}
}
```

## Standard signal dimensions

From the SDK + TelemetryDeck ingest:

- `type` — event name (the signal's `type`)
- `clientUser` — hashed user id
- `appID`, `isTestMode`
- `modelName`, `systemVersion`, `majorSystemVersion`
- `appVersion`, `buildNumber`
- `locale`, `countryCode`
- `isAppStore`, `isSimulator`, `isDebug`
- `platform` (iOS/macOS/...)

Custom params (the `parameters:` dict in `TelemetryDeck.signal(...)`) appear as dimensions named after the key. Typos silently return zero rows — `events` subcommand or a quick `topN` of `type` confirms what's actually firing.

## Multi-dimension groupBy

`groupBy` takes an array of DimensionSpecs. Result has one row per unique combination:

```json
"dimensions":[
  {"type":"default","dimension":"license","outputName":"license"},
  {"type":"default","dimension":"majorSystemVersion","outputName":"os"}
]
```

Sort order is not implicit — sort client-side or use `limitSpec` (see docs if needed).
