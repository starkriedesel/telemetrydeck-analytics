---
name: telemetrydeck-analytics
description: Query a TelemetryDeck app's product analytics to answer questions about DAU/MAU, retention, event counts, funnels, cohorts, and pipeline health. Use whenever the user asks a "how are users actually using this app" question that needs live data from TelemetryDeck, or is diagnosing signal-ingestion issues (missing events, opt-in anomalies, appID filters). Ships a self-contained CLI with OS-native secret storage — no repo-local `.env`, no secrets on disk outside the OS credential store.
version: 0.6.2
---

# TelemetryDeck Analytics

Answers product-analytics questions against the TelemetryDeck v3 API via the bundled `tdq` CLI. Stdlib-only, cross-platform, OS-native secret storage, no `.env`, no repo-local files.

This skill ships as part of the `telemetrydeck-analytics` Claude plugin. While the plugin is enabled, the `tdq` wrapper is on `PATH`, so you can call it directly as `tdq <subcommand>`. If the wrapper isn't available (e.g. running outside Claude Code), fall back to `python3 "$SKILL_DIR/tdq.py" <subcommand>`.

## User-invokable slash skills

The plugin exposes focused slash commands for common tasks — prefer them when the user's intent maps cleanly to one:

| Slash command | What it does |
|---|---|
| `/telemetrydeck-analytics:setup` | First-run login + app picker. |
| `/telemetrydeck-analytics:doctor` | End-to-end health check. |
| `/telemetrydeck-analytics:apps [list\|use\|add\|remove\|refresh]` | Manage registered apps. |
| `/telemetrydeck-analytics:dau [args]` | Daily active users. |
| `/telemetrydeck-analytics:mau [args]` | Monthly active users. |
| `/telemetrydeck-analytics:groupby <dim> [args]` | Breakdown by a dimension. |
| `/telemetrydeck-analytics:events` | Schema discovery (7d + 30d event counts). |
| `/telemetrydeck-analytics:signals [args]` | Top-N events, raw pipeline triage. |
| `/telemetrydeck-analytics:query <file or TQL>` | Run raw TQL. |
| `/telemetrydeck-analytics:report <question>` | Generate a markdown analytics report. |

This main skill handles the ambient / conversational case ("is the Pro share growing?", "why aren't signals arriving?") — it's model-invoked when a question needs analytics but doesn't match a slash command exactly.

## First-run setup (once per machine)

```bash
tdq login
```

Prompts for email and password, mints a bearer, then **lists the apps on your TelemetryDeck account and asks you to pick one**. No need to know the app UUID up front. If the listing endpoint isn't exposed for your account, the CLI falls back to a manual UUID prompt.

Accounts that sign in with Google/SSO have no password, so `login` can never succeed for them. Use a personal access token (dashboard user menu → Personal Access Tokens, paid plans only):

```bash
tdq login --pat                    # hidden prompt
echo "$TDPAT" | tdq login --pat    # or piped, for CI
```

The token is validated before it is stored, so a bad paste leaves any working credentials untouched. It is used until the API rejects it; there is no password to re-mint from, so when it expires (one year maximum) or is revoked, run `tdq login --pat` again.

Secrets (password + bearer) go into the OS-native store:
- **macOS** — Keychain via `security` (service `telemetrydeck-cli`).
- **Linux** — libsecret via `secret-tool` if installed (GNOME Keyring, KWallet-libsecret, etc.).
- **Windows / Linux without `secret-tool`** — file fallback at `<config-dir>/secrets.json` mode 0600, with a one-time stderr warning. Install `libsecret-tools` on Linux (`apt install libsecret-tools`) for a proper keyring.

Non-secret state (email, registered apps, token expiry) lives in a platform-appropriate config directory:
- macOS: `~/Library/Application Support/TelemetryDeckCLI/config.json`
- Linux: `$XDG_CONFIG_HOME/TelemetryDeckCLI/config.json` (or `~/.config/...`)
- Windows: `%APPDATA%\TelemetryDeckCLI\config.json`

After setup, every subcommand auto-refreshes the bearer on expiry or HTTP 401.

`$SKILL_DIR` = the absolute directory of this SKILL.md (inside the plugin at `skills/analytics/`). Use it to read bundled reference files (`tql/index.md`, `reference.md`); prefer the `tdq` PATH wrapper for invocations when it is available, otherwise fall back to `python3 "$SKILL_DIR/tdq.py"` as noted above.

Verify setup:

```bash
tdq doctor
```

Reports pass/fail for platform (+ which secret backend is active), config, secret store, auth, and a trivial query round-trip with remediation hints.

## Managing multiple apps

If your TelemetryDeck account has more than one app, the CLI registers them all at `login` time. Switch between them, add more, or remove entries with the `apps` subcommand:

```bash
tdq apps                       # list registered apps, * marks current
tdq apps use MyApp             # switch by display name
tdq apps use <uuid>            # switch by UUID
tdq apps use 2                 # switch by 1-based index from `apps`
tdq apps add <uuid> --name "New App" [--set-current]
tdq apps remove <uuid-or-name-or-index>
tdq apps refresh               # re-pull the list from the API
```

Every query command also accepts a one-off `--app-id <UUID>` flag that bypasses the current-app setting for a single invocation.

## Subcommands

| Command | Purpose |
|---|---|
| `login [--app-id UUID] [--pat [TOKEN]] [--reset]` | Email/password prompt, mint bearer, interactive app picker. `--app-id` skips the picker. `--pat` authenticates with a personal access token instead — required for SSO accounts. |
| `apps [list\|use\|add\|remove\|refresh]` | Manage the registered apps and switch the current one. Bare `apps` = list. |
| `logout` | Wipe stored secrets and config file. |
| `whoami` | Show user/org info (raw JSON). |
| `doctor` | End-to-end setup check. Fails if the round-trip returns zero rows. |
| `test` | Runs 3 known-good queries (timeseries, topN, groupBy), prints raw post-unwrap JSON + parsed row counts. Use when results look suspicious or after an API change. |
| `dau [--interval S\|--days N] [--event E]` | Daily active users (cardinality of `clientUser` per day). |
| `mau [--interval S\|--months N] [--event E]` | Monthly active users. |
| `groupby <dim> [--event E] [--interval S\|--days N] [--metric count\|users]` | Break down event count or user count by any dimension. |
| `events [--top N]` | Schema discovery: merged 7-day + 30-day event list with counts. |
| `signals [--days N] [--top N]` | Top-N event names — raw pipeline triage. |
| `insights` / `insight <id> [--days N]` | List / run saved dashboard insights. |
| `query <file\|-> [--include-test-mode]` | Run raw TQL from file or stdin. |

All query-producing commands share `--format table|csv|json` (default: `table` — markdown-ready for direct paste into a report), `--app-id <UUID>`, and `--raw`.

**`--raw` / `TDQ_RAW=1`**: prints the raw (post-envelope-unwrap) TelemetryDeck response to **stderr** before the formatted output. Reach for this the moment the formatted table says `(no rows)` — if the raw blob is non-empty, the parser dropped data (file a bug or re-check `_flatten_result`); if the raw blob itself is empty or a success-envelope with `"rows": []`, the query is genuinely empty and you need to widen the window or fix the filter.

**Named intervals** (`--interval`) accepted by `dau`, `mau`, `groupby`:
`last-Nd` for any N (e.g. `last-7d`, `last-30d`, `last-90d`), plus calendar-aware windows
`last-week`, `this-week`, `last-month`, `this-month`, `last-year`, `this-year`,
with aliases `wtd`, `mtd`, `ytd`. `--interval` wins over `--days` / `--months` when both are set.

**Period comparison** (`--compare prior-period`) on `dau`, `mau`, `groupby`: runs the same query against the immediately prior period, joins on the dimension (for `topN` / `groupBy`) or sums metrics (for `timeseries`), and emits one row per key with `current`, `prior`, `delta`, `pct` columns. For `last-Nd`, prior = the preceding N days. For `last-week`/`last-month`/`last-year`, prior = the completed period before that. For `this-X` to-date intervals, prior = the full last-X calendar period (note: this compares partial-to-full, which is usually what you want for "trending up?" questions but may understate growth early in a period — call this out in reports).

The `query` subcommand auto-injects the mandatory `appID` + `isTestMode=false` filter when the TQL has no `filter` key or sets it to the sentinel `{ "__auto_app_and_test_mode_filter__": true }`. If you write your own `filter`, include both selectors yourself — otherwise the result mixes other apps in the tenant and test-mode traffic.

## Worked example — "Is license mix shifting?"

```
User: "Has our Pro share grown month-over-month?"
```

Claude runs:

```bash
tdq groupby license --event App_launched \
  --interval last-30d --compare prior-period
```

Output:

```
| license | count_current | count_prior | delta | pct     |
|---------|---------------|-------------|-------|---------|
| Free    | 1823          | 1520        | 303   | +19.9%  |
| Pro     | 412           | 380         | 32    | +8.4%   |
| Trial   | 0             | 27          | -27   | —       |
```

Claude reports inline:

> **License mix, last 30d vs. prior 30d (opt-in sample):** Pro event share **moved from 19.7% → 18.4%** despite absolute Pro events rising +8.4% (412 vs 380). Free grew faster (+19.9%), diluting the Pro share. The Trial segment disappeared (27 → 0) — worth confirming with engineering whether the `Trial` license value is still emitted. Counts are `App_launched` events, not unique users; for user-share, re-run with `--metric users`.

## Worked example — "What's our license mix?"

```
User: "What's our Free/Pro split over the last 30 days?"
```

Claude runs:

```bash
tdq groupby license --event App_launched --days 30
```

Output:

```
| timestamp            | license | count |
|----------------------|---------|-------|
| 2026-03-19T00:00:00Z | Free    | 1823  |
| 2026-03-19T00:00:00Z | Pro     | 412   |
| 2026-03-19T00:00:00Z |         | 98    |
```

Claude reports inline:

> **License mix, last 30d (opt-in sample):** Free **78.2%** (1823), Pro **17.7%** (412), unknown/null **4.2%** (98). Counts are `App_launched` events, not unique users — use `--metric users` for unique-user share. Numbers reflect the opted-in cohort only; note any known opt-in bias when interpreting.

## Report structure (only if user asks for a saved report)

Default is inline in chat. If the user wants a file, **ask where to save it** — write to their absolute path. Suggest `~/Documents/TelemetryDeck/YYYY-MM-DD-<slug>.md` or `~/Downloads/`. Never create `analytics/`, `reports/`, or any new top-level directory in the current working directory.

Structure:

- **Question** — one sentence.
- **Top-line answer** — one paragraph, concrete numbers and date range.
- **Method** — CLI invocation or resolved TQL so the run is reproducible.
- **Caveats** — opt-in sample bias, test-mode exclusion, channel skew (App Store vs. direct), cohort window.
- **Raw result** (optional appendix).

## Triage heuristics

- **"DAU / MAU trend"** → `dau --interval last-30d`, `mau --interval last-6d` (or `mau --months 6`).
- **"Week-over-week growth?"** → `dau --interval last-7d --compare prior-period`.
- **"Is this month beating last month?"** → `dau --interval this-month --compare prior-period`. Remember: `this-*` compares partial-to-full; note the caveat in the report.
- **"License or version split"** → `groupby license --event App_launched --interval last-30d`, `groupby majorSystemVersion --metric users --interval last-7d`.
- **"License mix trend"** → `groupby license --event App_launched --interval last-30d --compare prior-period`.
- **"Which events exist?"** or **"is event X firing?"** → `events` (merged 7d + 30d table). If X is absent from 7d but present in 30d, the event stopped firing — likely gated by a user setting (classic failure: `@AppStorage` default not mirrored via `UserDefaults.register(defaults:)`, so `UserDefaults.bool(forKey:)` silently returns `false`).
- **"Where do users drop off?"** → write a `funnel` TQL query and pipe to `query -`. The CLI doesn't ship a funnel recipe yet; funnel steps vary enough that a bad default is worse than raw JSON.
- **"Crashes"** → not this skill. Sentry or equivalent.

## Raw TQL workflow

For queries the recipes don't cover, pipe TQL to `query -`:

```bash
cat <<'EOF' | tdq query - --format table
{
  "queryType": "topN",
  "granularity": "all",
  "aggregations": [{"type": "eventCount", "name": "count"}],
  "metric": {"type": "numeric", "metric": "count"},
  "dimension": {"type": "default", "dimension": "countryCode", "outputName": "country"},
  "filter": { "__auto_app_and_test_mode_filter__": true },
  "relativeIntervals": [{"beginningDate":{"component":"day","offset":-30,"position":"beginning"},
                          "endDate":{"component":"day","offset":0,"position":"end"}}],
  "threshold": 20
}
EOF
```

For TQL syntax, **start at `tql/index.md`** and follow the routing table to the specific topic (query types, filters, aggregators, dimensions, intervals, granularity, funnel, retention, recipes). Each file is focused — load only what you need. `reference.md` covers only the HTTP / auth / async mechanics; it does not document TQL.

### Saved-insight TQL shape (what the server emits)

When the `insight` subcommand resolves a saved insight to TQL, the server returns a slightly different shape than the recipes above: `baseFilters: "thisApp"` instead of an explicit `appID` selector, and aggregators like `thetaSketch` for user counts. **Both styles are valid** — the server accepts either. The explicit-filter style in the recipes is more portable (works across apps, machine-verifiable), the `baseFilters` style is what the dashboard emits. When porting a saved insight to a raw recipe, feel free to rewrite `baseFilters: "thisApp"` into the explicit `appID` + `isTestMode` selector pair without changing semantics.

## Debugging empty / weird results

When the formatted output shows `(no rows)` or doesn't match what you expect:

1. **Re-run with `--raw`** (or `TDQ_RAW=1`). The raw JSON goes to stderr. Compare it to the formatted output — if raw has data but formatted doesn't, the parser dropped rows.
2. **`tdq.py test`** — runs three known-good query shapes and dumps raw + parsed row counts. If all three show zero rows, the API may have changed its response envelope. If only one shape is broken, `_flatten_result` in `tdq.py` needs an update for that shape.
3. **`tdq.py doctor`** — asserts the 7-day round-trip has rows. A doctor failure with "0 rows" means either the app is genuinely empty or the parser regressed; `--raw` on any other command will tell you which.
4. **Widen the window** — `--days 30` or `--interval last-30d`. A 1-day window can legitimately be empty on a quiet app.
5. **Check the event name** — `tdq.py events` to see what's actually firing. Typos return zero silently.

## Guardrails

- **Never echo secrets.** Don't `cat .env`, don't print Keychain contents, don't paste the bearer into chat, reports, or commits.
- **Never write to CWD without permission.** Default is stdout; saved reports go to a user-chosen absolute path.
- **Abort runaway queries.** The CLI caps at 30 polls / 120s wallclock. If a query hits those limits, tighten `--days`, narrow `--event`, or drop the row cap.
- **Respect opt-in bias.** Analytics samples are self-selected by users who left the setting on. Every report drawn from opt-in-gated events must state that caveat.

## Project-specific context

App-specific analytics knowledge (custom event names, known pipeline quirks, license-parameter semantics) belongs in the host project's `CLAUDE.md`, not in this skill. Keep the skill portable.

## Known limitations

- `security add-generic-password -w <value>` (macOS) briefly exposes the password to `ps` on the local machine. Same-user visibility only, but a real limitation; fix requires a pty workaround.
- File-backend fallback (Windows, Linux without `libsecret`) stores secrets at `<config-dir>/secrets.json` with mode 0600. Disk-readable by the same user, which is strictly weaker than a keyring. Install `libsecret-tools` on Linux for a proper keyring; on Windows, prefer env vars (`TELEMETRYDECK_TOKEN`, `TELEMETRYDECK_PASSWORD`) until Credential Manager support lands.
- No funnel/retention recipes yet. Raw TQL via `query -` works.
- `--compare prior-period` for `this-X` intervals compares partial-to-full (e.g. this-month-to-date vs. all of last month). Call this out in reports where it matters; reach for `last-Nd --compare prior-period` if you want like-for-like windows.
