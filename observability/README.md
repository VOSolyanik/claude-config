# Local OpenTelemetry stack for Claude Code

Claude Code exports metrics and events over OTLP (OpenTelemetry Protocol); this stack stores them on the
machine and shows them in Grafana. Nothing leaves localhost: every port is bound to 127.0.0.1.

```text
Claude Code ──OTLP gRPC :4317──► OTel Collector ──OTLP──► Prometheus :9090  (metrics, 90 days)
                                                └─OTLP──► Loki :3100        (events, 30 days)
                                 Grafana :3000 ◄── both, dashboard "Claude Code"
```

| File | What |
| --- | --- |
| `docker-compose.yml` | the four services, pinned image tags, ports on 127.0.0.1 |
| `otel-collector.yaml` | OTLP in; drops account identity; metrics to Prometheus's OTLP receiver, events to Loki's |
| `prometheus.yml` | nothing to scrape: metrics arrive over OTLP |
| `loki.yaml` | single-process Loki on the filesystem, retention 30 days |
| `grafana/` | datasources and the dashboard, provisioned from files |

## Run

```bash
cd ~/tools/claude-config/observability    # the live clone, like every other part of the config
read -rs P && printf 'GF_SECURITY_ADMIN_PASSWORD=%s\n' "$P" > .env && unset P && chmod 600 .env   # git-ignored
docker compose up -d
open http://127.0.0.1:3000                # log in as admin; anonymous access is off
```

Grafana reads the password only when it creates its database. To change it later, recreate the volume
(dashboards and datasources come from files, nothing else is kept there):

```bash
docker compose rm -sf grafana && docker volume rm claude-otel_grafana-data && docker compose up -d grafana
```

## Point Claude Code at it

These keys work only in user settings (project and local settings ignore them), so they belong in the
personal layer's `env`:

```json
{
  "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
  "OTEL_METRICS_EXPORTER": "otlp",
  "OTEL_LOGS_EXPORTER": "otlp",
  "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
  "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
  "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "cumulative",
  "OTEL_METRIC_EXPORT_INTERVAL": "10000",
  "OTEL_LOG_TOOL_DETAILS": "1"
}
```

- `cumulative` is required: the CLI defaults to delta, and Prometheus stores cumulative counters.
- `OTEL_LOG_TOOL_DETAILS=1` puts real skill, agent, plugin and MCP server names on cost and token metrics
  (otherwise `custom` / `third-party`) and adds tool parameters, such as full Bash commands, to events.
- Prompts, responses and tool content stay out (`OTEL_LOG_USER_PROMPTS` and friends are not set).
- `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` does not interfere: the exporter runs in the main process.

## Why the counters look the way they do

- Each Claude Code process keeps its own cumulative counters, so `session.id` stays on metrics: without
  it, concurrent sessions would overwrite one series.
- Prometheus runs with `created-timestamp-zero-ingestion`, so a session's counter starts from zero and
  its first sample counts.
- Panels use `increase(…[range] anchored)` (`promql-extended-range-selectors`): plain `increase()`
  extrapolates, and on short sessions it overstated cost by about half in the first test.
- Account identity (`user.id`, `user.email`, `user.account_uuid`, `user.account_id`, `organization.id`)
  is dropped in the collector.

## Panels

1. $ per day by model — `claude_code.cost.usage`, a client-side estimate at list price.
2. $ by skill and by subagent — the same metric by `skill_name` / `agent_name`.
3. Tokens by type and cache hit — `claude_code.token.usage`; cache hit = cacheRead / (input + cacheRead + cacheCreation).
4. Edit acceptance — `claude_code.code_edit_tool.decision`.
5. Gate blocks — events: `tool_decision` with `source=hook`, and blocking `hook_execution_complete`.
   Deny rules from user settings are reported as `user_reject`, the same as a manual refusal, so they are not counted.
6. PR cycle time — deferred: needs GitHub data, not OTel.

## Check that data arrives

```bash
curl -s http://127.0.0.1:9090/api/v1/label/__name__/values | jq -r '.data[] | select(startswith("claude"))'
curl -s -G http://127.0.0.1:3100/loki/api/v1/query_range --data-urlencode 'query={service_name="claude-code"}' | jq '.data.result | length'
```
