---
name: sirvir-budget
description: "Token usage monitoring and budget skill. Documents how to read Hermes state.db for real usage data, track spending against a monthly budget, set alert thresholds (75% yellow, 90% orange, 100% red), and suggest upgrades/downgrades based on utilization. Complements the turbofit core skill — turbofit serves models, sirvir-budget tracks what they cost."
version: 1.1.0
author: SouthpawIN
license: MIT
tags: [budget, token-usage, cost-tracking, state-db, alerts, spending, turbofit]
metadata:
  hermes:
    tags: [budget, token-usage, cost-tracking, state-db, alerts, spending]
    related_skills: [turbofit, sirvir-research, sirvir-serve]
  changelog: |
    1.1.0 (2026-06-29): Fixed state.db path (per-profile, not ~/.hermes). Replaced sqlite3 CLI queries with Python sqlite3 (sqlite3 CLI not installed). Added beta-period effective-cost computation. Updated budget config reference to live file. Added fleet-wide aggregate query.
    1.0.0 (2026-06-26): Initial split from turbofit monolith. Wraps the state.db usage queries, monthly budget tracking, alert thresholds, and upgrade/downgrade suggestion logic.
---

# Sirvir-Budget — Token Usage Monitoring & Budget

This skill is the **cost layer** of the Sirvir model fleet. The turbofit core skill serves models; sirvir-budget tracks what they actually cost — reading real usage from Hermes `state.db`, projecting monthly spend, alerting when thresholds are hit, and suggesting upgrades or downgrades based on utilization. The daily 6:00 AM research cron delegates the budget check to this workflow.

## When to use

Load this skill when any of the following are needed:

- The daily budget check is due (part of the 6:00 AM research cron)
- A user asks "what's my budget?", "how much have I spent?", "what's my projection?"
- A budget alert threshold (75% / 90% / 100%) was hit and needs surfacing
- An upgrade/downgrade suggestion is needed based on utilization
- A model swap's cost impact needs to be estimated before committing
- The user wants to change the monthly budget or alert thresholds
- Cache savings need to be reported (models with prompt caching)
- The user has a prepaid/GPU-time beta phase and a later token-billed production phase that need an apples-to-apples budget forecast
- The user expects post-upgrade caching improvements and needs a cache-aware effective-cost-per-million planning ladder

Trigger phrases: "what's my budget", "how much have I spent", "monthly projection", "budget alert", "cost tracking", "token usage", "cache savings", "can I afford <model>", "suggest a downgrade", "am I underutilizing".

## Data source: Hermes state.db

All usage data comes from Hermes's own SQLite database — real input/output/cache tokens, real cost, per model, per request. The database lives **per-profile**, not at `~/.hermes/state.db`. The correct path is:

```
~/.hermes/profiles/<profile_name>/state.db
```

For Sirvir's own profile: `~/.hermes/profiles/sirvir/state.db`

> **Important**: The `sqlite3` CLI is NOT installed on this system. All queries must use Python's `sqlite3` module via `python3 -c "..."`. The queries below use this pattern.

### Quick schema check

```bash
# Check what tables exist and their schemas
python3 -c "
import sqlite3
db = sqlite3.connect('~/.hermes/profiles/sirvir/state.db')
tables = db.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()
print('Tables:', [t[0] for t in tables])
for t in tables:
    cols = db.execute(f'PRAGMA table_info({t[0]})').fetchall()
    print(f'  {t[0]}: {[(c[1],c[2]) for c in cols]}')
"
```

### Standard queries (per-profile)

All queries use the `sessions` table — the `usage` table does not exist in this deployment. The `sessions` table has `input_tokens`, `output_tokens`, `cache_read_tokens`, `actual_cost_usd`, and `estimated_cost_usd`.

```bash
# Per-profile: this month's sessions by model
python3 -c "
import sqlite3
db = sqlite3.connect('~/.hermes/profiles/sirvir/state.db')
db.row_factory = sqlite3.Row
rows = db.execute('''
SELECT COALESCE(model,'') AS model,
       COUNT(*) AS sessions,
       SUM(input_tokens) AS input_tok,
       SUM(output_tokens) AS output_tok,
       SUM(cache_read_tokens) AS cache_tok,
       ROUND(100.0 * SUM(cache_read_tokens) / NULLIF(SUM(input_tokens) + SUM(cache_read_tokens), 0), 2) AS cache_rate_pct,
       ROUND(SUM(COALESCE(actual_cost_usd, estimated_cost_usd, 0)), 2) AS cost_usd
FROM sessions
GROUP BY COALESCE(model,'')
ORDER BY (input_tok + output_tok + cache_tok) DESC
''').fetchall()
for r in rows:
    print(f'{r[\"model\"]:30s} sessions={r[\"sessions\"]:3d}  in={r[\"input_tok\"]:>10,}  out={r[\"output_tok\"]:>10,}  cache={r[\"cache_tok\"]:>10,}  rate={r[\"cache_rate_pct\"]:>6.1f}%  cost=\${r[\"cost_usd\"]:>8.2f}')
"
```

### Fleet-wide aggregate (all profiles)

```bash
# Aggregate across all profiles
python3 -c "
import sqlite3, os, glob
total_in = total_out = total_cache = 0
total_sessions = 0
for path in sorted(glob.glob('~/.hermes/profiles/*/state.db')):
    prof = path.split('/')[-2]
    db = sqlite3.connect(path)
    rows = db.execute('SELECT SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens), COUNT(*) FROM sessions').fetchone()
    if rows[3]:
        total_in += rows[0] or 0
        total_out += rows[1] or 0
        total_cache += rows[2] or 0
        total_sessions += rows[3]
        print(f'{prof:20s}  sessions={rows[3]:3d}  in={rows[0] or 0:>12,}  out={rows[1] or 0:>10,}  cache={rows[2] or 0:>10,}')
print(f'{\"FLEET TOTAL\":20s}  sessions={total_sessions:3d}  in={total_in:>12,}  out={total_out:>10,}  cache={total_cache:>10,}')
total_tokens = total_in + total_out + total_cache
cache_rate = 100.0 * total_cache / (total_in + total_cache) if (total_in + total_cache) > 0 else 0
print(f'Total tokens: {total_tokens:,}')
print(f'Cache rate: {cache_rate:.1f}%')
"
```

### Effective cost during beta (subscription billing)

When the fleet runs on a subscription provider (e.g. a subscription provider), per-token costs in state.db show $0. Compute effective cost from the subscription price divided by actual token volume:

```bash
# Effective cost per 1M tokens during beta
python3 -c "
import sqlite3, os, glob, yaml

# Read budget config
with open('~/.hermes/profiles/sirvir/skills/turbofit/references/budget-config.yaml') as f:
    cfg = yaml.safe_load(f)

sub_cost = cfg['beta']['subscription_cost_usd']
period = cfg['beta']['billing_period']

# Fleet-wide token total
total_tokens = 0
for path in glob.glob('~/.hermes/profiles/*/state.db'):
    db = sqlite3.connect(path)
    row = db.execute('SELECT SUM(input_tokens + output_tokens + cache_read_tokens) FROM sessions').fetchone()
    if row[0]:
        total_tokens += row[0]

if total_tokens > 0 and sub_cost > 0:
    effective_per_1m = (sub_cost / total_tokens) * 1_000_000
    print(f'Subscription: \${sub_cost}/{period}')
    print(f'Total tokens: {total_tokens:,}')
    print(f'Effective cost: \${effective_per_1m:.4f} per 1M tokens')
else:
    print(f'Subscription cost unknown or zero tokens. Set beta.subscription_cost_usd in budget-config.yaml.')
"
```

## Budget config

The budget configuration lives at `turbofit/references/budget-config.yaml`. This is the **live source of truth** for the monthly budget, alert thresholds, scorecard anchors, planning bands, and beta/production tracking.

```bash
# Read the current budget
cat ~/.hermes/profiles/sirvir/skills/turbofit/references/budget-config.yaml
```

Key fields:

| Field | Purpose |
|-------|---------|
| `monthly_budget_usd` | The user's monthly API budget |
| `alert_thresholds` | Yellow (75%), Orange (90%), Red (100%) of budget |
| `scorecard` | Operational thresholds for the weekly post-upgrade scorecard |
| `planning_bands` | Optimistic/base/conservative $/1M rates for cache-aware forecasting |
| `beta` | Beta period tracking (active, provider, subscription cost) |
| `production` | Post-beta provider and cutover date |

### Adjusting the budget

The user can change the budget at any time. Sirvir recalibrates projections and alerts against the new value.

```bash
# Edit the config directly
# Then re-run the budget check to confirm the new thresholds
python3 ~/.hermes/profiles/sirvir/skills/turbofit/scripts/research-models.py
cat ~/.hermes/profiles/sirvir/skills/turbofit/references/research-report.md
```

## Alert thresholds

| Threshold | Severity | Message template |
|-----------|----------|------------------|
| **75% of budget** | Yellow (WARN) | "You're trending toward your budget limit. Current projection: $X of $Y." |
| **90% of budget** | Orange (WARN) | "Budget nearly exhausted. Recommend switching to cheaper alternatives." |
| **100% of budget** | Red (CRITICAL) | "Budget exhausted. Switching to free endpoints only (NIM)." |

### Alert workflow

1. **Daily check** (6:00 AM research cron): compute month-to-date spend + projection
2. **Compare projection against thresholds**: if projected monthly spend crosses 75% / 90% / 100%, raise the corresponding alert
3. **Surface to Discord** (real-time for WARN/CRITICAL) and the consolidated log
4. **At 100%**: recommend switching to free NIM endpoints only — `serve auto main --free`

```bash
# Force a budget check on demand
python3 ~/.hermes/profiles/sirvir/skills/turbofit/scripts/research-models.py
# The report includes a budget status section
grep -A 10 "Budget" ~/.hermes/profiles/sirvir/skills/turbofit/references/research-report.md
```

## Over-budget suggestions

When spend is trending over budget, suggest specific swaps that save money. Always know the cost — these suggestions come from live pricing in `references/model-database.yaml` (kept current by sirvir-research).

| Situation | Suggestion template |
|-----------|---------------------|
| Premium main is the cost driver | "Switching main from GLM 5.2 ($0.95/$3.00) to DeepSeek V4 Pro (free via NIM) would save $X/month." |
| Aux usage is high | "Your aux usage is high. Routing more to the local MoE (free) would cut API costs by Y%." |
| Context bloat | "Consider reducing aux context to 512K — saves cache tokens without quality loss." |
| Pairing inefficiency | "Your current main+aux pairing costs $Z/M blended. Switching to <alt pair> costs $W/M — saves $V/month." |

## Underutilization suggestions

When spend is well under budget, suggest upgrades that improve quality without exceeding the budget.

| Situation | Suggestion template |
|-----------|---------------------|
| Low API spend | "You're only using 40% of your API budget. You could afford GLM 5.2 for main instead of DeepSeek V4 Flash — better quality for $X/month more." |
| Local GPU idle | "Your local GPU is underutilized. You could run a larger aux model (35B MoE) instead of the current 27B dense — same speed, more intelligence." |
| Context headroom | "You have headroom for a 1M context upgrade on main. Current: 262K. Cost: $0 additional (local)." |

## Cache-aware budget ladders and beta-vs-production comparisons

When the user is comparing:
- a prepaid or GPU-time-limited beta phase (for example Ollama subscription / GPU-time billing)
- against a later token-billed production phase

Do not compare the two phases with raw token totals alone.

Instead:
1. Treat beta as a workload-shape and caching-validation phase.
2. Convert production planning into an effective `$ / 1M tokens` rate.
3. Build an optimistic / base / conservative budget ladder from that effective rate.
4. Distinguish clearly between current measured cache performance and expected post-upgrade cache performance.

If the user provides small-scale and large-scale forecasts, test them for consistency by converting both to effective `$ / 1M` and comparing the band. If the bands are close, the forecast is directionally coherent.

Recommended planning shape for this class of case:
- planning anchor: base case
- stretch target: optimistic case
- do-not-be-surprised ceiling: conservative case

The session-derived reference ladder lives at `references/cache-aware-budget-ladder.md`.
For recurring weekly reviews after rollout, use `references/post-upgrade-budget-scorecard.md`.
For reconstructed or explained three-tier routing economics (premium / default / cheap lanes), use `references/lane-based-cost-model.md`.
For focused verification of budget-reference edits when no canonical test suite exists, run `scripts/verify_budget_docs.py` via a temporary `/tmp/hermes-verify-*` wrapper and report the result explicitly as ad-hoc verification rather than suite green.

## Beta provider substitution and budget tracking

When the fleet is running a beta period on a subscription provider (substituting for a per-token provider), budget tracking should note that:

- subscription provider costs are subscription-based (weekly quota, not per-token), so token cost in state.db may show $0 or a flat rate
- The real cost comparison subscription vs per-token should use the subscription cost divided by actual token volume
- When the beta ends and the fleet switches to Nous, per-token costs will appear in state.db
- Budget projections during beta should use the anticipated Nous pricing, not the current subscription $0

### Computing effective cost during beta

Since state.db shows $0.00 during subscription billing, compute effective cost manually:

```bash
# Effective cost per 1M tokens
python3 -c "
import sqlite3, glob, yaml

# Read budget config for subscription cost
with open('~/.hermes/profiles/sirvir/skills/turbofit/references/budget-config.yaml') as f:
    cfg = yaml.safe_load(f)

sub_cost = cfg['beta']['subscription_cost_usd']
period = cfg['beta']['billing_period']

# Fleet-wide token total
total_tokens = 0
for path in glob.glob('~/.hermes/profiles/*/state.db'):
    db = sqlite3.connect(path)
    row = db.execute('SELECT SUM(input_tokens + output_tokens + cache_read_tokens) FROM sessions').fetchone()
    if row[0]:
        total_tokens += row[0]

if total_tokens > 0 and sub_cost > 0:
    effective_per_1m = (sub_cost / total_tokens) * 1_000_000
    print(f'Subscription: \${sub_cost}/{period}')
    print(f'Total tokens: {total_tokens:,}')
    print(f'Effective cost: \${effective_per_1m:.4f} per 1M tokens')
    # Compare against planning bands
    for band, rate in cfg['planning_bands'].items():
        status = 'BELOW' if effective_per_1m < rate else 'ABOVE'
        print(f'  vs {band} (\${rate}/1M): {status}')
else:
    print('Set beta.subscription_cost_usd in budget-config.yaml to enable effective-cost tracking.')
"
```

### Beta-to-production forecast

When the user asks for a production budget forecast during beta:

1. Read the current fleet-wide token volume from state.db
2. Read the production pricing from `model-database.yaml` for the planned post-cutover models
3. Apply the planning bands from `budget-config.yaml` (optimistic/base/conservative)
4. Present the forecast as a range, not a single number

```bash
# Production budget forecast from current beta usage
python3 -c "
import sqlite3, glob, yaml

# Fleet-wide token total
total_in = total_out = total_cache = 0
for path in glob.glob('~/.hermes/profiles/*/state.db'):
    db = sqlite3.connect(path)
    row = db.execute('SELECT SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens) FROM sessions').fetchone()
    if row[0]:
        total_in += row[0] or 0
        total_out += row[1] or 0
        total_cache += row[2] or 0

total_tokens = total_in + total_out + total_cache

# Read planning bands
with open('~/.hermes/profiles/sirvir/skills/turbofit/references/budget-config.yaml') as f:
    cfg = yaml.safe_load(f)

bands = cfg['planning_bands']
budget = cfg['monthly_budget_usd']

print(f'Current fleet volume: {total_tokens:,} tokens')
print(f'Monthly budget: \${budget}')
print()
print('Production forecast (post-beta, per-token billing):')
for label, rate in bands.items():
    monthly = (total_tokens / 1_000_000) * rate
    pct = (monthly / budget) * 100 if budget > 0 else 0
    print(f'  {label:15s} \${rate}/1M → \${monthly:.2f}/month ({pct:.0f}% of budget)')
"
```

## Daily budget check (part of 6:00 AM research cron)

The budget check is steps 2-5 and 9 of the daily research workflow (owned by sirvir-research):

1. **Read actual usage** from Hermes `state.db` (real tokens, cache hit rate, cost)
2. **Project monthly cost** for each model based on actual usage patterns
3. **Project pairing costs** with aux offset (40-85% of tokens route to aux)
4. **Report cache savings** for models that support cache reads
5. **Check budget status** — spend vs monthly budget, alert if threshold hit
6. (sirvir-research continues with HuggingFace scan, database update, GitHub sync)

```bash
# The research script does all of this; the budget section is in the report
python3 ~/.hermes/profiles/sirvir/skills/turbofit/scripts/research-models.py

# Read just the budget-relevant sections
cat ~/.hermes/profiles/sirvir/skills/turbofit/references/research-report.md | sed -n '/Budget/,/^##/p'
```

## On-demand budget report

When the user asks "what's my budget?" or "how much have I spent?":

```bash
# 1. Run the research script (fetches fresh pricing + reads state.db)
python3 ~/.hermes/profiles/sirvir/skills/turbofit/scripts/research-models.py

# 2. Read the report
cat ~/.hermes/profiles/sirvir/skills/turbofit/references/research-report.md

# 3. Quick fleet-wide aggregate
python3 -c "
import sqlite3, glob
total_in = total_out = total_cache = 0
for path in glob.glob('~/.hermes/profiles/*/state.db'):
    db = sqlite3.connect(path)
    row = db.execute('SELECT SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens) FROM sessions').fetchone()
    if row[0]:
        total_in += row[0] or 0
        total_out += row[1] or 0
        total_cache += row[2] or 0
total = total_in + total_out + total_cache
cache_rate = 100.0 * total_cache / (total_in + total_cache) if (total_in + total_cache) > 0 else 0
print(f'Fleet total: {total:,} tokens, cache rate: {cache_rate:.1f}%')
"

# 4. Compare against budget
grep monthly_budget_usd ~/.hermes/profiles/sirvir/skills/turbofit/references/budget-config.yaml
```

Present to the user:
```
Budget status: $X spent of $Y monthly (Z%)
Projection: $W by end of month (V% of budget)
Status: 🟢 green / 🟡 yellow (75%+) / 🟠 orange (90%+) / 🔴 red (100%+)
Top cost driver: <model> at $A/month
Cache savings: $B (C% of input tokens hit cache)
Suggestion: <upgrade or downgrade recommendation>
```

## Cost tracking philosophy

From AGENTS.md:

1. **Local models**: Zero API cost. VRAM and electricity are the only costs.
2. **API fallback**: Tracked via Hermes Insights (state.db) — real input/output/cache tokens, real cost.
3. **Monthly projection**: Based on actual usage patterns from Hermes state.db.
4. **Cache savings**: Reported for models that support prompt caching (78-99% savings on cache hits).
5. **Budget management**: Tracked against monthly budget with alerts at 75% / 90% / 100%.

**Prefer free endpoints.** Local → NIM free → paid API. Always know the cost.

## Integration with turbofit core

- **turbofit** owns the `scripts/research-models.py` script that reads `state.db` and generates the budget report, and the `references/budget-config.yaml` config file. This skill documents the budget workflow that sits on top of them.
- The daily research cron is registered in Sirvir's profile config; this skill is the budget-check portion of that cron.
- Pricing data used for cost projections comes from `references/model-database.yaml`, kept current by sirvir-research's OpenRouter sync.
- Budget-driven model swaps are executed via turbofit's `serve main`/`serve aux`/`serve auto main --free` commands.

## Cross-references

- **sirvir-research** — owns the daily research cron and the OpenRouter pricing sync that keeps `model-database.yaml` pricing current; sirvir-budget's projections depend on that pricing data
- **sirvir-serve** — when a user wants an external app endpoint, sirvir-budget determines whether a paid API model fits the budget or a free/local option is the right call
- **sirvir-scale** — API fallback (Beefy Step 4+) has a cost; sirvir-budget tracks whether the fallback is free (NIM) or paid (Nous/OR), and a Step 7 fallback to paid API can trigger a budget alert
- **sirvir-bench** — benchmark scores justify upgrade/downgrade suggestions: a cheaper model that benchmarks within 5% of a premium one is a budget win
- **turbofit** (core skill) — `SKILL.md` documents the dynamic model database, the research script, and `serve auto main --free`; this skill is the cost-tracking workflow that sits on top of them
