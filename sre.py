#!/usr/bin/env python3
"""
sre.py - A simple interactive CLI assistant that proposes Prometheus Alert Rules.
read-only: nothing is ever applied, output is display-only.
"""

import os
import json
import math
import sys

import requests
from openai import OpenAI

# ---------------------------------------------------------------------------
# Configuration (everything from env)
# ---------------------------------------------------------------------------
PROM_URL = os.environ.get("PROM_URL", "http://localhost:9090").rstrip("/")
MEMORY_FILE = os.environ.get("MEMORY_FILE", "memory.json")

PROVIDERS = {
    "gapgpt": {
        "base_url": os.environ.get("GAPGPT_BASE_URL", "https://api.gapgpt.app/v1"),
        "key_env": "GAPGPT_API_KEY",
    },
    "openrouter": {
        "base_url": os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        "key_env": "OPENROUTER_API_KEY",
    },
}

# Mutable runtime state
STATE = {
    "provider": os.environ.get("PROVIDER", "openrouter"),
    "model": os.environ.get("MODEL", "openai/gpt-4o-mini"),
}


# ---------------------------------------------------------------------------
# Provider client (OpenAI-compatible)
# ---------------------------------------------------------------------------
def client():
    p = PROVIDERS[STATE["provider"]]
    key = os.environ.get(p["key_env"])
    if not key:
        raise SystemExit(f"API key not found. Set the env var: {p['key_env']}")
    return OpenAI(base_url=p["base_url"], api_key=key)


def list_models():
    p = PROVIDERS[STATE["provider"]]
    key = os.environ.get(p["key_env"], "")
    r = requests.get(
        f"{p['base_url']}/models",
        headers={"Authorization": f"Bearer {key}"},
        timeout=20,
    )
    r.raise_for_status()
    return [m["id"] for m in r.json().get("data", [])]


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------
def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_memory(mem):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(mem, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Prometheus access (read-only)
# ---------------------------------------------------------------------------
def prom_get(path, params=None):
    r = requests.get(f"{PROM_URL}{path}", params=params or {}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(data.get("error", "prometheus error"))
    return data["data"]


def prom_query(promql):
    return prom_get("/api/v1/query", {"query": promql})


def prom_query_range(promql, days, step=None):
    end = prom_get("/api/v1/query", {"query": "time()"})  # server time
    end_ts = float(end["result"][0]["value"][1])
    start_ts = end_ts - days * 86400
    if step is None:
        # Aim for ~1500 points across the whole range
        step = max(60, int(days * 86400 / 1500))
    data = prom_get(
        "/api/v1/query_range",
        {"query": promql, "start": start_ts, "end": end_ts, "step": step},
    )
    return data, step


def discover():
    """Scans the monitoring environment into memory.json."""
    mem = {}
    # jobs
    jobs = prom_get("/api/v1/label/job/values")
    mem["jobs"] = jobs
    # metric names
    metrics = prom_get("/api/v1/label/__name__/values")
    mem["metrics"] = metrics
    # metric type and help
    try:
        meta = prom_get("/api/v1/metadata")
        mem["metric_meta"] = {
            k: v[0] for k, v in meta.items() if v
        }  # {name: {type, help, unit}}
    except Exception:
        mem["metric_meta"] = {}
    # targets
    try:
        targets = prom_get("/api/v1/targets")
        mem["targets"] = [
            {
                "job": t["labels"].get("job"),
                "instance": t["labels"].get("instance"),
                "health": t.get("health"),
            }
            for t in targets.get("activeTargets", [])
        ]
    except Exception:
        mem["targets"] = []
    save_memory(mem)
    return mem


# ---------------------------------------------------------------------------
# Statistics and backtest (judgment-free tools: numbers only)
# ---------------------------------------------------------------------------
def _percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100.0
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def _all_values(range_data):
    """Flattens all values of all series into a single list."""
    vals = []
    for series in range_data.get("result", []):
        for _, v in series.get("values", []):
            try:
                vals.append(float(v))
            except ValueError:
                pass
    return vals


def tool_range_stats(promql, days=7):
    data, step = prom_query_range(promql, days)
    vals = sorted(_all_values(data))
    if not vals:
        return {"error": "no data returned", "promql": promql}
    return {
        "promql": promql,
        "days": days,
        "step_seconds": step,
        "series_count": len(data.get("result", [])),
        "points": len(vals),
        "min": vals[0],
        "max": vals[-1],
        "mean": sum(vals) / len(vals),
        "p50": _percentile(vals, 50),
        "p90": _percentile(vals, 90),
        "p95": _percentile(vals, 95),
        "p99": _percentile(vals, 99),
    }


def tool_backtest(promql, threshold, op=">", for_minutes=5, days=7):
    """Simple simulation of an alert over historical data. Returns numbers only, no judgment."""
    data, step = prom_query_range(promql, days)
    need = max(1, math.ceil(for_minutes * 60 / step))  # consecutive points needed to fire

    def hit(v):
        v = float(v)
        return v > threshold if op == ">" else v < threshold

    fires = 0
    fire_points = 0
    total_points = 0
    for series in data.get("result", []):
        streak = 0
        firing = False
        for _, v in series.get("values", []):
            total_points += 1
            try:
                ok = hit(v)
            except ValueError:
                ok = False
            if ok:
                streak += 1
                if streak >= need:
                    fire_points += 1
                    if not firing:
                        fires += 1
                        firing = True
            else:
                streak = 0
                firing = False

    return {
        "promql": promql,
        "threshold": threshold,
        "op": op,
        "for_minutes": for_minutes,
        "days": days,
        "step_seconds": step,
        "series_count": len(data.get("result", [])),
        "fire_count": fires,  # how many times the alert turned on
        "fire_points": fire_points,  # total points spent in firing state
        "total_points": total_points,
        "fire_ratio": round(fire_points / total_points, 4) if total_points else 0,
    }


def tool_instant(promql):
    data = prom_query(promql)
    out = []
    for s in data.get("result", [])[:50]:
        out.append({"metric": s.get("metric", {}), "value": s.get("value", [None, None])[1]})
    return {"promql": promql, "results": out}


def tool_targets():
    mem = load_memory()
    if "targets" in mem:
        return mem["targets"]
    data = prom_get("/api/v1/targets")
    return [
        {"job": t["labels"].get("job"), "instance": t["labels"].get("instance"), "health": t.get("health")}
        for t in data.get("activeTargets", [])
    ]


# ---------------------------------------------------------------------------
# Tool definitions for the model
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "range_stats",
            "description": "Statistics over the historical data of a PromQL: min/max/mean and percentiles. Numbers only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "promql": {"type": "string"},
                    "days": {"type": "integer", "default": 7},
                },
                "required": ["promql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "backtest",
            "description": "Simulates a threshold over historical data and reports how many times it would fire and how noisy it was.",
            "parameters": {
                "type": "object",
                "properties": {
                    "promql": {"type": "string"},
                    "threshold": {"type": "number"},
                    "op": {"type": "string", "enum": [">", "<"], "default": ">"},
                    "for_minutes": {"type": "integer", "default": 5},
                    "days": {"type": "integer", "default": 7},
                },
                "required": ["promql", "threshold"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "instant_query",
            "description": "Returns the current value of a PromQL.",
            "parameters": {
                "type": "object",
                "properties": {"promql": {"type": "string"}},
                "required": ["promql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_targets",
            "description": "List of Prometheus targets (job/instance/health).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

DISPATCH = {
    "range_stats": lambda a: tool_range_stats(a["promql"], a.get("days", 7)),
    "backtest": lambda a: tool_backtest(
        a["promql"], a["threshold"], a.get("op", ">"), a.get("for_minutes", 5), a.get("days", 7)
    ),
    "instant_query": lambda a: tool_instant(a["promql"]),
    "get_targets": lambda a: tool_targets(),
}


SYSTEM_PROMPT = """You are an SRE assistant. The user gives a vague request and you propose a Prometheus Alert Rule.
The monitoring environment is available to you from memory.json (jobs, metrics with their types, targets).

How to work:
1. Find the relevant metric from memory. Pay attention to the metric type: a counter must be used with rate(), a gauge directly, a histogram with histogram_quantile.
2. Use range_stats to get statistics over the last 7 days (percentiles).
3. Propose several candidate thresholds and test them with backtest over the same data.
4. If it is noisy (high fire_count), raise the threshold or for_duration and backtest again.
5. If usage is inherently high and stable (e.g. even p50 is near the ceiling), recommend increasing capacity/resources instead of an alert.

The tools return only raw numbers and make no judgment; the decision is yours.
Nothing is applied -- only display the final Alert Rule as YAML together with a short rationale and a backtest summary.
"""


def run_agent(user_request):
    mem = load_memory()
    mem_summary = {
        "jobs": mem.get("jobs", []),
        "metric_meta": mem.get("metric_meta", {}),
    }
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": "memory.json:\n" + json.dumps(mem_summary, ensure_ascii=False)[:8000]},
        {"role": "user", "content": user_request},
    ]
    cli = client()
    for _ in range(12):  # iteration cap to avoid infinite loops
        resp = cli.chat.completions.create(
            model=STATE["model"], messages=messages, tools=TOOLS
        )
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if not msg.tool_calls:
            print("\n" + (msg.content or "") + "\n")
            return
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            print(f"  - {tc.function.name}({json.dumps(args, ensure_ascii=False)})")
            try:
                result = DISPATCH[tc.function.name](args)
            except Exception as e:
                result = {"error": str(e)}
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, ensure_ascii=False)}
            )
    print("Reached iteration cap without a final result.")


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------
HELP = """Commands:
  /query <promql>     Run a query directly (current value)
  /targets            List targets
  /discover           Scan the environment and save to memory.json
  /model [name]       Show/change the model
  /provider [name]    Show/change the provider (gapgpt | openrouter)
  /help               This help
  /exit               Quit

Any other text is sent to the model as a request.
Example: create alerts for the databases
"""


def handle_slash(line):
    parts = line.split(maxsplit=1)
    cmd = parts[0]
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "/help":
        print(HELP)
    elif cmd == "/exit":
        sys.exit(0)
    elif cmd == "/query":
        if not arg:
            print("usage: /query <promql>")
        else:
            print(json.dumps(tool_instant(arg), ensure_ascii=False, indent=2))
    elif cmd == "/targets":
        print(json.dumps(tool_targets(), ensure_ascii=False, indent=2))
    elif cmd == "/discover":
        mem = discover()
        print(f"saved: {len(mem.get('metrics', []))} metrics, {len(mem.get('jobs', []))} jobs, {len(mem.get('targets', []))} targets")
    elif cmd == "/provider":
        if arg in PROVIDERS:
            STATE["provider"] = arg
            print(f"provider = {arg}")
        elif arg:
            print(f"invalid provider. one of: {', '.join(PROVIDERS)}")
        else:
            print(f"current provider: {STATE['provider']}")
    elif cmd == "/model":
        if arg:
            STATE["model"] = arg
            print(f"model = {arg}")
        else:
            print(f"current model: {STATE['model']}")
            try:
                models = list_models()
                print("available models (first 30):")
                for m in models[:30]:
                    print(f"  {m}")
            except Exception as e:
                print(f"error fetching model list: {e}")
    else:
        print(f"unknown command: {cmd}  (/help)")


def main():
    print("SRE Alert Assistant - read-only. /help for help.")
    print(f"provider={STATE['provider']}  model={STATE['model']}  prom={PROM_URL}\n")
    while True:
        try:
            line = input("sre> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line.startswith("/"):
            try:
                handle_slash(line)
            except SystemExit:
                break
            except Exception as e:
                print(f"error: {e}")
        else:
            try:
                run_agent(line)
            except Exception as e:
                print(f"error: {e}")


if __name__ == "__main__":
    main()
