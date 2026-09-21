# Funnel queries

`queryType: "funnel"` counts users who completed an ordered sequence of steps. Each step is a filter; a user "passes" step N if at least one signal in the query window matches that filter AND they passed step N−1.

## Shape

```json
{
  "queryType": "funnel",
  "granularity": "all",
  "steps": [
    {"type":"selector","dimension":"type","value":"App_launched"},
    {"type":"selector","dimension":"type","value":"Model_downloaded"},
    {"type":"selector","dimension":"type","value":"Transcription_started"},
    {"type":"selector","dimension":"type","value":"Transcription_completed"}
  ],
  "filter": { "__auto_app_and_test_mode_filter__": true },
  "relativeIntervals": [{
    "beginningDate":{"component":"day","offset":-30,"position":"beginning"},
    "endDate":      {"component":"day","offset":0,"position":"end"}
  }]
}
```

## Result

One row with a count per step:

```json
[{
  "result": {
    "steps": [
      {"stepNumber":1,"count":4230},
      {"stepNumber":2,"count":1810},
      {"stepNumber":3,"count":1642},
      {"stepNumber":4,"count":1598}
    ]
  }
}]
```

Compute drop-off client-side: `step2.count / step1.count`, etc.

## Step filters

Each step is any filter shape (see `filters.md`). Common:

- Single event: `{"type":"selector","dimension":"type","value":"..."}`
- Event + parameter: `{"type":"and","fields":[{"type":"selector","dimension":"type","value":"X"},{"type":"selector","dimension":"license","value":"Pro"}]}`
- One of several events: `{"type":"in","dimension":"type","values":["A","B"]}`

## Semantics that bite

- **Ordering is per-user, not per-signal.** A user who emits step-3 before step-2 still counts for step-2 as long as both exist in the window.
- **No "within X minutes" constraint.** Funnel counts across the whole query window. If you need "within one session", slice with a shorter `relativeIntervals` and run multiple queries.
- **`granularity: "all"` is standard.** Bucketed funnels return per-bucket step counts which rarely match product intuition.
- **User identity = `clientUser`.** If your app sends anonymous-then-identified signals for the same user, they count as two users.

## Combining with filters

Step filters narrow which signals progress the funnel; the top-level `filter` narrows the entire signal population first. Apply cohort predicates (license, platform) at the top level, step predicates in `steps`:

```json
"filter": {"type":"and","fields":[
  {"type":"selector","dimension":"appID","value":"<UUID>"},
  {"type":"selector","dimension":"isTestMode","value":"false"},
  {"type":"selector","dimension":"license","value":"Pro"}
]},
"steps": [
  {"type":"selector","dimension":"type","value":"App_launched"},
  {"type":"selector","dimension":"type","value":"Purchase_completed"}
]
```

= "Pro users: how many who launched also completed a purchase in last 30d."
