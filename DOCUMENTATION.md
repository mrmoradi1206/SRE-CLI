# SRE Alert Assistant — Documentation

An interactive command-line assistant for Site Reliability Engineering. You give it a vague
request (for example, "create alerts for the databases") and it inspects **real Prometheus
data** to propose a Prometheus **Alert Rule**.

The tool is **strictly read-only**: it never applies, writes, or modifies anything in
Prometheus or Alertmanager. Its only output is a suggestion printed to your terminal that you
review and decide what to do with.

---

## Table of contents

1. [Design philosophy](#1-design-philosophy)
2. [How it works end to end](#2-how-it-works-end-to-end)
3. [Installation](#3-installation)
4. [Configuration (environment variables)](#4-configuration-environment-variables)
5. [Providers and models](#5-providers-and-models)
6. [The `memory.json` file](#6-the-memoryjson-file)
7. [Slash commands](#7-slash-commands)
8. [Tools the model can call](#8-tools-the-model-can-call)
9. [The agent loop](#9-the-agent-loop)
10. [Running in Docker](#10-running-in-docker)
11. [Example session](#11-example-session)
12. [Limitations](#12-limitations)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. Design philosophy

The core principle is a clean split of responsibility:

- **Tools provide data, never judgment.** Each tool (`range_stats`, `backtest`,
  `instant_query`, `get_targets`) returns raw numbers only — percentiles, fire counts,
  ratios. A tool never decides whether a threshold is "good" or whether a metric is
  "noisy".
- **The model makes every decision.** Which metric is relevant, what the threshold should
  be, how much noise is acceptable, whether to recommend an alert at all versus a capacity
  increase — all of that reasoning lives in the model, informed by the numbers the tools
  return.
- **Read-only by construction.** There is no code path that mutates Prometheus or
  Alertmanager. The worst possible outcome is a bad suggestion on screen, which a human
  reviews before doing anything.

---

## 2. How it works end to end

When you type a request such as `create alerts for the databases`, the assistant runs an
agent loop:

1. **Find the metric.** Using the monitoring environment stored in `memory.json` (jobs,
   metric names, metric types, targets), the model picks the metric most relevant to the
   request. Metric type matters here: a `counter` must be wrapped in `rate()`, a `gauge` is
   used directly, and a `histogram` is used through `histogram_quantile`.
2. **Gather statistics.** The model calls `range_stats` to fetch the last 7 days of data and
   compute its distribution: min, max, mean, and the p50/p90/p95/p99 percentiles.
3. **Propose candidate thresholds.** Based on those statistics the model picks several
   candidate thresholds.
4. **Backtest.** Each candidate is run through `backtest`, which replays the same historical
   data and reports how many times the alert would have fired (`fire_count`) and how much of
   the time it would have been firing (`fire_ratio`).
5. **Tune.** If a candidate is noisy, the model raises the threshold or the `for_duration`
   and backtests again.
6. **Decide.** If usage is inherently high and stable (for example, even p50 sits near the
   ceiling), the model recommends increasing capacity/resources instead of an alert.
   Otherwise it emits the final alert rule.
7. **Output.** The final Alert Rule is printed as YAML, together with a short rationale and a
   summary of the backtest. Nothing is applied.

---

## 3. Installation

Requirements: Python 3.9+ (3.12 recommended) and a reachable Prometheus instance.

```powershell
pip install -r requirements.txt
```

Dependencies are intentionally minimal:

- `openai` — the OpenAI-compatible client used to talk to both providers.
- `requests` — HTTP calls to Prometheus and to the providers' model-list endpoint.

---

## 4. Configuration (environment variables)

All configuration is read from environment variables. API keys are **only** read from the
environment — never from code or a config file.

| Variable | Default | Purpose |
|----------|---------|---------|
| `PROM_URL` | `http://localhost:9090` | Base URL of the Prometheus server |
| `MEMORY_FILE` | `memory.json` | Path to the memory file |
| `PROVIDER` | `openrouter` | Active provider (`gapgpt` or `openrouter`) |
| `MODEL` | `openai/gpt-4o-mini` | Active model id |
| `OPENROUTER_API_KEY` | — | API key for OpenRouter |
| `GAPGPT_API_KEY` | — | API key for GapGPT |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Override OpenRouter base URL |
| `GAPGPT_BASE_URL` | `https://api.gapgpt.app/v1` | Override GapGPT base URL |

Example (PowerShell):

```powershell
$env:OPENROUTER_API_KEY = "sk-..."
$env:PROM_URL = "http://localhost:9090"
python sre.py
```

Example (bash):

```bash
export OPENROUTER_API_KEY="sk-..."
export PROM_URL="http://localhost:9090"
python sre.py
```

---

## 5. Providers and models

Two AI providers are supported, both of which speak the OpenAI-compatible API:

- **GapGPT** — base URL `https://api.gapgpt.app/v1`, key from `GAPGPT_API_KEY`.
- **OpenRouter** — base URL `https://openrouter.ai/api/v1`, key from `OPENROUTER_API_KEY`.

The active provider is chosen by `PROVIDER` (or switched at runtime with `/provider`). The
model list is fetched **live** from the provider's `/models` endpoint via `/model` (with no
argument), so you always see what the provider currently offers. Switch the active model with
`/model <id>`.

> Note: the assistant relies on **function/tool calling**. Pick a model that supports tool
> calling. The default `openai/gpt-4o-mini` is a safe, inexpensive choice on OpenRouter.

---

## 6. The `memory.json` file

`memory.json` stores a snapshot of the monitoring environment so the model has context
without re-scanning Prometheus on every request. It is produced by the `/discover` command
and contains:

| Key | Contents |
|-----|----------|
| `jobs` | All values of the `job` label |
| `metrics` | All metric names |
| `metric_meta` | Per-metric `{type, help, unit}` from Prometheus metadata |
| `targets` | Active targets as `{job, instance, health}` |

`metric_meta` is the most important part: it tells the model whether a metric is a counter,
gauge, histogram, or summary, which determines the correct PromQL shape.

This file is **derived data** — regenerate it with `/discover` whenever the environment
changes. It is the only file the tool writes.

---

## 7. Slash commands

Slash commands let you work directly, without involving the model.

| Command | Description |
|---------|-------------|
| `/query <promql>` | Run an instant query and print the current value(s) |
| `/targets` | List Prometheus targets (job / instance / health) |
| `/discover` | Scan the environment and save it to `memory.json` |
| `/model [name]` | With no argument, print the current model and the live model list. With a name, switch the active model |
| `/provider [name]` | With no argument, print the current provider. With `gapgpt` or `openrouter`, switch provider |
| `/help` | Show the command reference |
| `/exit` | Quit |

Any input that does **not** start with `/` is sent to the model as a natural-language
request.

---

## 8. Tools the model can call

These are exposed to the model as OpenAI function-calling tools. They return raw numbers
only.

### `range_stats(promql, days=7)`
Fetches the historical range of a PromQL expression and returns its distribution.

Returns: `step_seconds`, `series_count`, `points`, `min`, `max`, `mean`, `p50`, `p90`,
`p95`, `p99`.

The step is chosen automatically to produce roughly 1500 points across the range, with a
floor of 60 seconds.

### `backtest(promql, threshold, op=">", for_minutes=5, days=7)`
Replays historical data and simulates an alert: a fire begins once the condition
(`value op threshold`) has held for `for_minutes` continuously.

Returns: `fire_count` (how many distinct times it fired), `fire_points` (total points spent
firing), `total_points`, and `fire_ratio` (`fire_points / total_points`).

### `instant_query(promql)`
Returns the current value(s) of a PromQL expression (up to 50 series).

### `get_targets()`
Returns the list of active targets (from memory if available, otherwise live).

---

## 9. The agent loop

`run_agent()` drives a standard tool-calling loop:

1. Build the message list: the system prompt, a compact `memory.json` summary (jobs +
   metric metadata), and the user's request.
2. Call the model with the tool definitions attached.
3. If the model returns tool calls, execute each one, append the JSON result as a `tool`
   message, and loop again.
4. If the model returns plain text (no tool calls), print it as the final answer and stop.

The loop is capped at 12 iterations to prevent runaways. Each tool call is echoed to the
terminal (for example, `· backtest({"promql": "...", "threshold": 80})`) so you can watch the
reasoning unfold.

---

## 10. Running in Docker

The provided `Dockerfile` and `docker-compose.yml` run the assistant interactively in a
container with TTY/stdin attached, keys passed from the environment, and `memory.json`
mounted as a volume.

```powershell
$env:OPENROUTER_API_KEY = "sk-..."
docker compose run --rm sre
```

Use `docker compose run` (not `up`) so that stdin/TTY are attached for interactive use.

To reach a Prometheus instance that is already running in another compose project, uncomment
the `networks` block in `docker-compose.yml` and set `PROM_URL` to the in-network address
(for example `http://prometheus:9090`).

---

## 11. Example session

```
$ python sre.py
SRE Alert Assistant — read-only. /help for help.
provider=openrouter  model=openai/gpt-4o-mini  prom=http://localhost:9090

sre> /discover
saved: 312 metrics, 8 jobs, 14 targets

sre> create alerts for the databases
  · range_stats({"promql": "pg_stat_activity_count", "days": 7})
  · backtest({"promql": "pg_stat_activity_count", "threshold": 180, "for_minutes": 5})
  · backtest({"promql": "pg_stat_activity_count", "threshold": 200, "for_minutes": 10})

Proposed alert rule:

groups:
  - name: database
    rules:
      - alert: PostgresHighConnections
        expr: pg_stat_activity_count > 200
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "High number of PostgreSQL connections"

Rationale: p95 over the last 7 days was 165 and p99 was 188. A threshold of 200 with a
10-minute "for" fired 0 times in the backtest (fire_ratio 0.0), so it is well above normal
noise while still catching genuine saturation.
```

---

## 12. Limitations

This is a deliberately simple version. Known trade-offs:

- **Lightweight backtest.** The simulation is a straight `value op threshold` check with a
  consecutive-points rule for `for`. It does not reproduce full PromQL/Prometheus evaluation
  semantics (counter resets, rate windows, staleness, etc.).
- **7-day window.** Seven days captures only one of each weekday, which can be short for
  metrics with strong weekly seasonality.
- **Resolution effects.** Long ranges are downsampled to keep the point count manageable;
  percentiles computed on downsampled data can differ from percentiles on raw samples. The
  `step_seconds` is always reported so this is visible.
- **No input sanitization.** Prometheus data is passed to the model as-is. This version does
  not defend against prompt injection from label values.
- **Tool-calling dependency.** Models without reliable function calling will not work well.

---

## 13. Troubleshooting

**`API key not found. Set the env var: ...`**
The key for the active provider is not in the environment. Set `OPENROUTER_API_KEY` or
`GAPGPT_API_KEY` (matching your `PROVIDER`).

**`error: ... Connection refused` / Prometheus errors**
Check `PROM_URL` and that Prometheus is reachable from where the tool runs (especially inside
Docker — use the in-network hostname, not `localhost`).

**Empty stats / `no data returned`**
The PromQL matched no series for the time range. Run `/discover` to refresh `memory.json`, or
verify the metric exists with `/query <promql>`.

**Model returns no tool calls / poor results**
Switch to a model that supports tool calling: `/model` to list, then `/model <id>`.
