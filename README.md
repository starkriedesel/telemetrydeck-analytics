# telemetrydeck-analytics

A Claude Code plugin (and skill) that lets agents work with [TelemetryDeck](https://telemetrydeck.com) product analytics. Ask in plain English, get a real query, a real table, and an honest caveat.

## How it works

Once the plugin is installed, Claude knows there's an analytics skill sitting behind the `/telemetrydeck-analytics:*` slash commands and a CLI called `tdq` on `PATH`.

When you ask something ambient — "is the Pro share growing?", "why aren't signals arriving?", "how did last week compare to the week before?" — the `analytics` skill auto-invokes. Claude picks the right entry point (`tdq dau`, `tdq groupby license`, `tdq query -` with hand-written TQL, …), runs it, and hands you back a markdown table with the opt-in-cohort caveat attached.

When you want a specific one-shot, the namespaced slash commands do the right thing directly: `/telemetrydeck-analytics:doctor` for a health check, `/telemetrydeck-analytics:dau --last 30d`, `/telemetrydeck-analytics:report "how is retention trending?"`, and so on.

Under the hood it's a stdlib-only Python CLI that talks to TelemetryDeck's v3 API. Secrets live in your OS-native secret store (macOS Keychain, Linux libsecret, Windows/file fallback with a loud warning) — never in a `.env`. A progressive-disclosure TQL reference tree means Claude reads the exact sub-page it needs instead of pasting a full spec into context.

## Installation

### Claude Code

The repo doubles as its own single-plugin marketplace. Register it, then install:

```bash
/plugin marketplace add agenkin/telemetrydeck-analytics
/plugin install telemetrydeck-analytics@telemetrydeck-analytics
```

Or non-interactively:

```bash
claude plugin marketplace add agenkin/telemetrydeck-analytics
claude plugin install telemetrydeck-analytics@telemetrydeck-analytics
```

### skills.sh

Pulls the skill files including the bundled `tdq.py` — Claude uses it automatically via the `$SKILL_DIR` fallback. No slash commands and no `tdq` on `PATH`, but all queries work:

```bash
npx skills add agenkin/telemetrydeck-analytics
```

### Local checkout (for development)

```bash
git clone https://github.com/agenkin/telemetrydeck-analytics
claude plugin marketplace add ./telemetrydeck-analytics
claude plugin install telemetrydeck-analytics@telemetrydeck-analytics
```

## First-run setup

Inside a Claude Code session, run:

```
/telemetrydeck-analytics:setup
```

Claude walks you through email + password, mints a bearer, then shows a numbered picker of every app visible on your account. Pick one and you're done. Secrets land in your OS-native secret store (macOS Keychain / Linux libsecret / file fallback); nothing touches your repo.

**Signing in with Google/SSO?** Your account has no password to exchange for a bearer, so use a [personal access token](https://telemetrydeck.com/docs/api/api-token/) instead — generate one from the dashboard user menu, then `tdq login --pat` and paste it at the hidden prompt.

Prefer to do it from your shell? The `tdq` CLI exposes the same flow — it's on `PATH` once the plugin is enabled:

```bash
tdq login                          # same interactive flow
tdq login --pat                    # SSO accounts: paste a personal access token
tdq apps add <uuid>                # register another app later
tdq apps use <name|uuid|index>     # switch current app
```

## The Basic Workflow

1. **setup** — First run. Login, discover apps, pick the current one. Or run `/telemetrydeck-analytics:setup`.

2. **doctor** — Before asking real questions, verify the round-trip. Asserts non-empty rows and points at `TDQ_RAW=1` on failure.

3. **ask** — Ask Claude a product-analytics question in plain English. The `analytics` skill auto-invokes.

4. **narrow** — When the answer needs something specific, reach for a focused command: `:dau`, `:mau`, `:groupby <dim>`, `:events`, `:signals`, `:query`.

5. **report** — For a compact markdown writeup (top-line, method, result, interpretation, caveats), use `/telemetrydeck-analytics:report <question>`.

6. **iterate** — If a query comes back empty or suspicious, re-run with `TDQ_RAW=1` to see the raw HTTP response, or run `tdq test` to verify the API shape hasn't drifted.

## What's Inside

### Slash commands

**Setup + ops**
- **setup** — Interactive first-run login and app picker.
- **doctor** — End-to-end health check. Asserts non-empty rows, surfaces a clear remediation path on failure.
- **apps** — List, switch, add, or remove registered apps (supports UUID / name / 1-based index).

**Metrics**
- **dau** — Daily active users. Uses `userCount` on a day granularity, not the deprecated `cardinality`.
- **mau** — Monthly active users over the last N calendar months.
- **groupby** — Break down count or users by any dimension (license, version, `isAppStore`, platform, custom payload keys).

**Schema + raw**
- **events** — Schema discovery: merged 7-day and 30-day event counts so you can see what's firing.
- **signals** — Top-N events, raw pipeline triage.
- **query** — Run arbitrary TQL from a file or stdin. Claude consults `skills/analytics/tql/index.md` first.

**Narrative**
- **report** — Turn a question into a compact markdown report: top-line, method, result, interpretation, caveats.

### The `tdq` CLI

`bin/tdq` is on Claude's `PATH` whenever the plugin is enabled. Every slash command is a thin wrapper around it, and you can call it yourself from any shell Claude opens:

```bash
tdq doctor
tdq dau --last 30d
tdq groupby license --last 30d
tdq events
tdq query path/to/query.json
tdq test                          # 3 known-good queries, asserts shapes
TDQ_RAW=1 tdq signals --last 7d   # raw HTTP response → stderr
```

### Progressive-disclosure TQL reference

Under `skills/analytics/tql/`, one file per topic:

```
index.md       # router — read first
query-types.md # timeseries / topN / groupBy / scan / funnel / retention
filters.md     # selector, and, or, not, in, regex, interval
aggregators.md # userCount, thetaSketch, eventCount, filtered, doubleSum
dimensions.md  # dimension specs, extraction functions
intervals.md   # ISO-8601 intervals + relative-window shorthand
granularity.md # day / week / month / all
funnel.md      # funnel queries end-to-end
retention.md   # retention queries end-to-end
recipes.md     # copy-paste starting points
```

Claude reads `index.md` first and only opens the sub-page it actually needs. Your token budget thanks you.

## Philosophy

- **Secrets belong in the OS store, not in your repo.** No `.env`, ever. macOS Keychain, Linux libsecret, file fallback with 0600 + a loud stderr warning.
- **Progressive disclosure beats one giant prompt.** The TQL reference is a tree, not a wall. Claude opens the sub-page that matches the job.
- **Silent empty results are a bug.** `doctor` asserts non-empty rows. `tdq test` catches API shape drift before a live question returns `(no rows)`.
- **Every opt-in-gated report carries its caveat.** TelemetryDeck samples self-selected users. Reports say so, every time.
- **Gate policy inside services, not at call sites.** The skill decides whether an insight is a DAU question, a groupby, or a funnel — callers (you, Claude) don't have to pick the plumbing.

## Contributing

1. Fork the repo.
2. Create a branch (`feat/...` or `fix/...`).
3. Make changes. If you touch TQL handling, update the matching page under `skills/analytics/tql/`.
4. Run `claude plugin validate .` and `tdq test` against a real TelemetryDeck app.
5. Open a PR describing what changed and how you verified it.

Bigger changes (new slash commands, new aggregators, a new query type) — open an issue first so we can agree on the shape before you write the code.

## Updating

```bash
/plugin marketplace update telemetrydeck-analytics
```

Then `/plugin disable` + `/plugin enable` the plugin, or restart Claude Code.

## License

MIT — see [`LICENSE`](./LICENSE).

## Community

- **Issues**: https://github.com/agenkin/telemetrydeck-analytics/issues
- **TelemetryDeck**: https://telemetrydeck.com
- **Claude Code plugins**: https://code.claude.com/docs/en/plugins
- **skills.sh**: https://skills.sh
