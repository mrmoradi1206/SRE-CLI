# SRE Alert Assistant (simple version)

An interactive CLI assistant that proposes a Prometheus **Alert Rule** by looking at real
Prometheus data. Fully **read-only** — nothing is ever applied, output is display-only.

## Run (local)

```powershell
pip install -r requirements.txt

$env:OPENROUTER_API_KEY = "sk-..."      # or GAPGPT_API_KEY
$env:PROM_URL = "http://localhost:9090"
python sre.py
```

All settings come from environment variables. Copy `.env.example` to `.env` as a starting
point (see [DOCUMENTATION.md](DOCUMENTATION.md#4-configuration-environment-variables) for the
full list).

First, run `/discover` once so the environment is saved into `memory.json`. Then:

```
sre> /discover
sre> create alerts for the databases
```

## Slash commands (no model)

| Command | Action |
|---------|--------|
| `/query <promql>` | Current value of a query |
| `/targets` | List targets |
| `/discover` | Scan environment → memory.json |
| `/model [name]` | Show/change model (no arg: live model list) |
| `/provider [gapgpt\|openrouter]` | Change provider |
| `/help` | Help |
| `/exit` | Quit |

## docker-compose

```powershell
$env:OPENROUTER_API_KEY = "sk-..."
docker compose run --rm sre
```

To attach to an existing Prometheus network, uncomment the `networks` block in `docker-compose.yml`.

## How it works

The tools (`range_stats`, `backtest`, `instant_query`, `get_targets`) return only **raw numbers**.
The decisions (picking the metric, the threshold, the acceptable noise level) are made by the model.

See [DOCUMENTATION.md](DOCUMENTATION.md) for the full reference.
