# P6 observability foundation

This document is the operator contract for FootballAI staging observability. It
describes the pre-apply design only; no P6 Azure change has been applied.

## Audited Azure state (2026-08-25)

- `cae-footballai-stg` sends environment logs to the existing
  `log-footballai-stg` workspace through the legacy `log-analytics`
  destination. The customer/workspace IDs match.
- The workspace is `PerGB2018`, has 30-day retention, and is capped at 0.1 GB
  per day. It is reused; P6 creates no second workspace.
- No Application Insights component exists in `rg-footballai-stg`.
- No diagnostic settings exist on the audited Container Apps environment,
  apps, Service Bus namespace, PostgreSQL server, or Storage account. Native
  platform metrics are available without diagnostic settings.
- Successful app revisions and job executions existed, including a migration
  execution on 2026-08-25, but workspace `Usage` had no Container Apps data.
  `ContainerAppConsoleLogs_CL`, `ContainerAppSystemLogs_CL`, and
  `ContainerAppHTTPLogs_CL` did not exist, so this was neither an alternate
  table name nor ordinary ingestion delay.
- The workspace has `disableLocalAuth = true` and PostgreSQL remained
  `Stopped` (public access `Disabled`); this audit did not start it.

## Legacy logging diagnosis and remediation history

This diagnosis is retained because it is the reason for the architecture, not
because the workaround it first suggested was adopted.

- **Original state.** Container Apps logs destination = `log-analytics`; Log
  Analytics `disableLocalAuth = true`.
- **Observed.** Zero Container Apps ingestion. No `ContainerApp*` tables were
  ever created despite real app revisions and job executions.
- **Root cause.** The legacy `log-analytics` destination ships console/system
  logs with the **workspace shared key** through the Azure Monitor **HTTP Data
  Collector API**. Disabling workspace local authentication rejects shared-key
  writes, so ingestion fails silently — no error surface, no tables.
- **First workaround considered (REJECTED).** Re-enable workspace local
  authentication (`local_authentication_enabled = true`) so the shared-key path
  works again.
- **Why rejected.** That legacy Container Apps → Log Analytics path is built on
  the Azure Monitor HTTP Data Collector API, which Microsoft is retiring;
  shared-key ingestion support ends **2026-09-14**. Rebuilding the P6 design on
  a path that is weeks from end-of-support, and re-exposing the workspace shared
  key to do it, is the wrong direction.
- **Chosen remediation.** Move the environment to the **Azure Monitor logs
  destination** and route console/system logs to the same workspace with an
  **Azure Monitor diagnostic setting**. Diagnostic-setting ingestion is
  platform-authenticated, so it works with workspace local auth **disabled** and
  needs no shared key. See **Logging destination decision** below for the full
  Option A vs Option B comparison.

### Logging destination decision (Option A vs Option B)

| | Option A — rejected | Option B — chosen |
|---|---|---|
| ACA logs destination | `log-analytics` | `azure-monitor` |
| Ingestion path | HTTP Data Collector API (shared key) | Diagnostic setting, platform-authenticated |
| Workspace shared key | required, re-exposed | not required, never retrieved |
| Workspace local auth | must be re-enabled (`false → true`) | stays disabled (`false`) |
| Platform lifecycle | Data Collector API support ends 2026-09-14 | current, Microsoft-recommended |
| Destination tables | `ContainerAppConsoleLogs_CL` (custom) | `ContainerAppConsoleLogs` (resource-specific) |

Option B is correct for August 2026: it is the only option that both restores
ingestion and keeps `disableLocalAuth = true`, and it does not build new work on
an ingestion API that reaches end-of-support in weeks. Terraform confirms the
switch is an in-place update of the managed environment (0 destroy, 0 replace);
`logs_destination` and `log_analytics_workspace_id` are not ForceNew in
azurerm 4.81.0.

After apply, validate the logging fix with real workload activity and allow up
to several minutes for first-table creation. Do not claim the incident closed
until both console and system queries return new rows.

## Application logging

Cloud/staging workloads write newline-delimited JSON to stdout/stderr. Local and
test processes default to readable text. The stable vocabulary is:

| Class | Fields |
|---|---|
| Base | `timestamp`, `severity`, `service`, `environment`, `event`, `message` |
| Trace | `trace_id`, `span_id`, `request_id` |
| Analysis | `logical_analysis_id`, `run_id`, `attempt_number`, `stage`, `artifact_id` |
| Job/build | `job_execution_id`, `job_id`, `worker_id`, `code_revision`, `profile` |
| Outcome | `status`, `duration_ms`, `error_type`, `error_code` |

API request IDs are generated server-side unless the caller supplies a bounded,
safe `X-Request-ID`. Nginx generates and forwards the same ID. Context variables
carry correlation across async code without adding IDs to metric dimensions.

The redactor removes database passwords, authorization bearer values, storage
and Service Bus keys/connection-string values, sensitive mapping keys, and SAS
query values before formatting. Exceptions keep a sanitized stack trace and
safe error classification. Never add raw manifests, request headers, URLs with
query strings, or credentials as log/span fields.

## OpenTelemetry flow

```text
browser -> frontend/Nginx -> FastAPI request trace
                              -> analysis creation
                              -> Service Bus publish (traceparent/tracestate properties)
                              -> worker receives and attaches parent context
                              -> analysis/stage spans
                              -> artifact publication span
                              -> completion/error logs and metrics
```

The queue message body contract is unchanged. Only W3C `traceparent` and
`tracestate` application properties are added. Stable analysis identifiers are
kept in logs and useful trace attributes, never in metric dimensions.

`FOOTBALLAI_OTEL_MODE=disabled` is the local/test default. Staging uses
`azure_monitor`, the workspace-based `appi-footballai-stg` connection string,
and each workload's user-assigned identity with the narrow `Monitoring Metrics
Publisher` role scoped to the Application Insights component. Live Metrics,
performance counters, and offline disk storage are disabled. Health/readiness
URLs are excluded and parent-based 20% trace sampling bounds volume.

The component has local authentication disabled, so the connection string's
instrumentation key cannot authenticate ingestion — only the managed identity's
Entra token can. The connection string therefore carries endpoint/resource
discovery only, not a credential, and is passed to the workloads as a plain
`APPLICATIONINSIGHTS_CONNECTION_STRING` environment variable rather than a
Container Apps secret. This removes needless secret state and `listSecrets`
surface. It is still never emitted to logs or span attributes.

## Metrics

Application instruments:

- `analysis_started_total`, `analysis_succeeded_total`,
  `analysis_failed_total`, and `analysis_cancelled_total`;
- `analysis_duration_seconds` and `stage_duration_seconds`;
- `api_request_duration`;
- `worker_job_success` / `worker_job_failure`;
- `migration_job_success` / `migration_job_failure` and
  `migration_duration_seconds`.

Allowed dimensions are only `service`, `environment`, `status`, `stage`, and
`profile`. Code raises on `run_id`, `logical_analysis_id`, request IDs, or any
unreviewed dimension.

Use Azure-native metrics instead of duplicating them:

- Container Apps: `Requests`, `ResponseTime`, `Replicas`, `RestartCount`, CPU,
  and memory;
- Container Apps Jobs: `Executions`, `RestartCount`, CPU, and memory;
- Service Bus: `ActiveMessages`, `DeadletteredMessages`, incoming/outgoing
  messages, errors, and latency;
- PostgreSQL: `is_db_alive`, CPU, memory, storage, connections, failures, I/O,
  and deadlocks;
- Storage: transactions, availability, latency, ingress, and egress.

## Alerts and response

P6 defines stateful Azure metric alerts but deliberately configures no email,
SMS, phone, webhook, or personal recipient. Notification routing requires a
separate owner-approved action-group change.

| Alert | Why it matters | First checks |
|---|---|---|
| Service Bus dead-letter > 0 | Work is no longer processable | reason, worker errors, message contract, then deliberate replay |
| Service Bus active messages > 10 for 15m | Processing is falling behind | worker executions/scaling, PostgreSQL state, poison messages |
| API 5xx > 2 in 5m | User/control-plane requests are failing | readiness, revision health, dependencies, correlated trace |
| Worker execution failed | An analysis may be terminally failed or delayed | execution/run correlation, stage error, DB/queue/blob health |
| Migration execution failed | A deployment schema gate failed | stop deployment, migration trace/log, PostgreSQL readiness |

PostgreSQL resource-pressure alerts are deferred until a real workload baseline
exists; arbitrary student-environment thresholds would create noise.

## Operator view

Build one Azure Workbook after telemetry validation, or reproduce it from these
sources. A Terraform-managed workbook is intentionally deferred because its
large serialized JSON is brittle and provides little benefit before table
validation.

1. API requests/5xx and p95 response time: Container App native metrics plus
   `AppRequests`.
2. Analysis outcomes and duration: `AppTraces`/`AppMetrics`.
3. Worker and migration executions: Container Apps Jobs `Executions` metric.
4. Queue active/dead-letter messages: Service Bus native metrics.
5. PostgreSQL alive/CPU/storage/connections: PostgreSQL native metrics.
6. Container Apps replicas/restarts/CPU/memory: native metrics.
7. Recent correlated errors: `AppExceptions` and `AppTraces`.

## KQL queries

Workspace-based Application Insights uses `App*` tables. Container Apps platform
logs land in the **resource-specific** `ContainerAppConsoleLogs` and
`ContainerAppSystemLogs` tables (no `_CL` suffix), because the diagnostic
setting uses the `Dedicated` destination type.

> **Legacy note.** The retired `log-analytics` destination wrote custom
> `ContainerAppConsoleLogs_CL` / `ContainerAppSystemLogs_CL` tables (with `_s`
> columns) through the shared-key Data Collector API. Those `_CL` tables are
> **not** the target of this design and were never populated in staging. Only
> use them if querying a historical workspace that still ran the legacy path.

### Find a run across structured logs

```kusto
let RunId = "<run-id>";
AppTraces
| extend run_id=tostring(Properties["run_id"]), event=tostring(Properties["event"])
| where run_id == RunId or Message has RunId
| project TimeGenerated, SeverityLevel, AppRoleName, OperationId, event, Message, Properties
| order by TimeGenerated asc
```

Use the returned `OperationId` in `AppRequests`, `AppDependencies`, and
`AppExceptions` to follow the API-to-worker trace.

### Failed analyses and worker errors

```kusto
AppTraces
| extend event=tostring(Properties["event"]), run_id=tostring(Properties["run_id"]),
         stage=tostring(Properties["stage"]), error_code=tostring(Properties["error_code"])
| where event in ("analysis.failed", "queue.dead_lettered")
| project TimeGenerated, AppRoleName, OperationId, run_id, stage, error_code, Message
| order by TimeGenerated desc
```

### API 5xx

```kusto
AppRequests
| where TimeGenerated > ago(24h) and Success == false
| project TimeGenerated, Name, ResultCode, DurationMs, OperationId, AppRoleName
| order by TimeGenerated desc
```

### Migration failures

```kusto
AppTraces
| extend event=tostring(Properties["event"]), execution=tostring(Properties["job_execution_id"])
| where event == "migration.failed"
| project TimeGenerated, execution, OperationId, Message, Properties
| order by TimeGenerated desc
```

### Verify Container Apps console/system ingestion

Console logs carry the application's newline-delimited JSON in the `Log` column,
so `parse_json(Log)` exposes the structured fields:

```kusto
ContainerAppConsoleLogs
| where TimeGenerated > ago(1h)
| extend payload=parse_json(Log)
| project TimeGenerated, ContainerAppName, RevisionName, Stream,
          event=tostring(payload.event), run_id=tostring(payload.run_id), Log
| order by TimeGenerated desc
```

```kusto
ContainerAppSystemLogs
| where TimeGenerated > ago(1h)
| project TimeGenerated, ContainerAppName, RevisionName, Reason, Log
| order by TimeGenerated desc
```

If either table is absent after apply and fresh activity, re-check the
environment logs destination (`azure-monitor`), the diagnostic setting
(`diag-cae-footballai-stg` targeting the environment with `Dedicated`
destination type), the daily-cap status, and Azure resource health. Do **not**
re-enable workspace local authentication: the diagnostic-setting path is
platform-authenticated and does not use the workspace shared key.

## Cost and validation gates

- One workspace-based Application Insights component reuses the capped 30-day
  workspace; it does not create a second ingestion store.
- Trace sampling is 20%; health checks, live metrics, performance counters,
  debug logs, and offline telemetry storage are disabled.
- Logging stays at `INFO`; stage granularity is bounded and video/manifests are
  never emitted.
- Five native metric alerts add negligible telemetry volume. No availability
  web test or duplicate platform-metric export is added.
- The environment diagnostic setting routes only `ContainerAppConsoleLogs` and
  `ContainerAppSystemLogs` into the same capped workspace. `ContainerAppHTTPLogs`
  and `AllMetrics` are deliberately not enabled: HTTP request telemetry already
  arrives through Application Insights, and platform metrics are queryable and
  alertable natively without exporting them. Retention stays 30 days and the
  0.1 GB/day cap is unchanged.
- A review-only plan (`-refresh=false`, non-authoritative — a live plan needs
  PostgreSQL running) reports the environment switch and secret→env changes as
  in-place: **0 destroy, 0 replace**. `terraform apply` remains forbidden until
  an authoritative reviewed plan reports zero destroys and zero replacements and
  the operator explicitly approves the first P6 apply.
