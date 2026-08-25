# P5 — DevSecOps CI/CD & secretless Azure delivery

This document describes the GitHub Actions CI/CD and DevSecOps layer that
delivers FootballAI to Azure staging **without any long-lived Azure
credential**. It replaces the manual `Mac → docker build → ACR → terraform`
flow with a reviewed, gated pipeline.

```
Developer
  ↓
Pull Request → CI + Security → Review
  ↓
main
  ↓
GitHub Actions image build → OIDC build identity → ACR SHA images
  ↓
manual deploy workflow (explicit image_sha)
  ↓
OIDC plan identity → Terraform safety plan
  ↓
GitHub Environment approval
  ↓
OIDC deploy identity → fresh equivalent plan → Terraform apply
  ↓
migration → stable-FQDN smoke
```

No client secret participates anywhere in this path.

## Workflows

| File | Trigger | Purpose |
|---|---|---|
| `ci.yml` | PR, push to main | backend unit+contract, split-plane integration (PostgreSQL 16 + Azurite), frontend typecheck/lint/test/build, terraform fmt+validate |
| `security.yml` | PR, push, weekly cron | Gitleaks (full history), Semgrep SAST, pip-audit, npm audit, Trivy IaC |
| `build-images.yml` | push to main, manual | build+prove+scan+push 3 images to ACR via OIDC |
| `deploy-staging.yml` | manual (`image_sha`) | plan → approval → apply → migrate → smoke |

All third-party actions are pinned to full commit SHAs (with a `# vX.Y.Z`
comment). Top-level `permissions: contents: read`; only Azure jobs add
`id-token: write`, and only on non-PR triggers, so fork PRs never reach Azure.
The temporary P5 branch triggers, deploy-request nonce, smoke-only workflow and
OIDC diagnostic workflow were retired after build, plan and deploy OIDC had
each succeeded in the real staging delivery path.

## Secretless Azure OIDC

There is **no Azure client secret, no `AZURE_CREDENTIALS`, no ACR admin/password,
and zero GitHub secrets.** Three User-Assigned Managed Identities in
`rg-footballai-gha-oidc`, each federated to one GitHub environment:

| Identity | Environment | RBAC (least privilege) |
|---|---|---|
| `id-footballai-gha-build` | `staging-build` | `AcrPush` → ACR |
| `id-footballai-gha-plan` | `staging-plan` | `Reader` + narrow Container Apps/Jobs `listSecrets` custom actions → `rg-footballai-stg`; `Storage Blob Data Contributor` → `tfstate` container; `AcrPull` → ACR |
| `id-footballai-gha-deploy` | `staging` | `Contributor` → `rg-footballai-stg` only; `Storage Blob Data Contributor` → `tfstate` container; `AcrPull` → ACR |

`Contributor` deliberately lacks `Microsoft.Authorization/roleAssignments/write`.
A normal image deploy leaves `rbac.tf` role assignments unchanged, so this
suffices. An RBAC change in a plan is a **STOP** signal, never a reason to grant
Owner / User Access Administrator.

### OIDC subject format (immutable)

This repository emits **immutable** OIDC subjects — verified empirically from a
real Actions run, *not* from the customization API (which misleadingly reported
`use_immutable_subject=false`):

```
repo:azizbenammar7@126194752/FootballAi@1264402679:environment:<name>
```

The Azure federated credentials match this exact form. The bootstrap lives in
`infra/terraform/bootstrap/github-oidc/` (state key `footballai/github-oidc.tfstate`)
and is applied once by a human `az login`, never by CI.

## GitHub environments

| Environment | Reviewer | Deployment branches |
|---|---|---|
| `staging-build` | none | `main` |
| `staging-plan` | none | `main` |
| `staging` | `azizbenammar7` (required) | `main` |

Custom deployment branch policies (never `*`). `prevent_self_review` is disabled
because this is a single-maintainer project — otherwise the sole maintainer
could never approve a deployment; the manual approval gate itself is retained.

Non-secret identifiers are GitHub **variables** (repo-level + per-environment
`AZURE_CLIENT_ID`); see `AZURE_*`, `ACR_*`, `TF_STATE_*`.

## PostgreSQL regression protection (the P4 defect)

P4 shipped images whose `psycopg` had no usable `libpq` (`ImportError: no pq
wrapper available`) and it only surfaced against real Azure PostgreSQL.
`build-images.yml` now runs, inside each built **api** and **worker** image,
before push:

```python
import psycopg; from psycopg import pq
assert pq.__impl__ != 'python'   # a real compiled libpq impl, not the pure fallback
importlib.import_module(<app module>)
```

This fails the build offline, long before Azure — closing the gap that `alembic
heads` (a script-only check) left open.

## Terraform plan/apply governance

`infra/terraform/scripts/plan_policy.py` parses `terraform show -json` and:

1. **rejects any destroy or replace** (any action containing `delete`);
2. **rejects any create/update outside** the frontend/api/worker/migration
   workloads (protects PostgreSQL, VNet, Storage, Service Bus, ACR, Container
   Apps Environment, identities, role assignments, tfstate);
3. emits a **sanitized** address+action summary (never a value) and a
   deterministic `sha256`.

The **plan** job computes the hash; the **apply** job re-plans, recomputes it,
and refuses to apply unless it matches the approved plan — so a raw/sensitive
binary plan is never stored, yet apply provably matches what was reviewed.
Plan and apply are separate jobs; apply runs in the `staging` environment behind
the required-reviewer gate. `concurrency: footballai-staging-deploy` serializes
deploys and never cancels an in-flight apply.

Terraform refresh of a Container App or Job containing a `secret` block calls
the Azure `listSecrets` action even when only planning. The plan identity keeps
`Reader` and adds only `Microsoft.App/containerApps/listSecrets/action` and
`Microsoft.App/jobs/listSecrets/action` through a resource-group-scoped custom
role. It has no secret-mutation, resource-write, delete or IAM action.

## Migration & smoke

After apply, the existing `caj-footballai-stg-db-migrate` Container Apps Job is
started and awaited (fail on `Failed`/timeout). Then the pipeline smoke-checks
`https://<frontend>/healthz` and the proxied `https://<frontend>/api/ready`
(expects `database`/`object_storage`/`queue` ready). The internal API is never
exposed publicly. `run_e2e` optionally probes the analysis API surface; a full
`demo_fast` video run remains the P4-proven manual validation.

The smoke script resolves `properties.configuration.ingress.fqdn` from Azure,
which is the stable Container App hostname. Terraform's `frontend_fqdn` and
`api_fqdn` outputs have the same stable-ingress semantics; explicitly named
`*_latest_revision_fqdn` outputs expose revision-specific hostnames when needed.

## Rollback

Re-run `deploy-staging.yml` with an **older** `image_sha` whose images already
exist in ACR. This redeploys the previous application images through the same
plan → approval → apply path.

**Application rollback does not downgrade the database schema.** Alembic
`downgrade` is never run automatically; a schema rollback is a separate,
deliberate database decision. Verify the target image is compatible with the
current schema before rolling back.

## Vulnerability policy

- **Secrets (Gitleaks):** any finding fails; one documented allowlist entry for
  Microsoft's public Azurite emulator key (`.gitleaks.toml`).
- **SAST (Semgrep):** findings fail; one documented `# nosemgrep` on the
  container self-healthcheck loopback `urlopen` (hardcoded scheme+host, int port).
- **Dependencies:** `pip-audit` fails on any known vuln in the api/worker runtime
  locks; `npm audit` fails on HIGH+. Baseline was remediated
  (`python-multipart 0.0.22→0.0.31`, `react-router-dom 7.18.1→7.18.2`).
- **IaC (Trivy):** fails on HIGH/CRITICAL; `AZU-0012` accepted (`.trivyignore`) —
  storage is AAD-only with shared keys disabled, so a Deny default action would
  break the Container Apps data plane and CI state access.
- **Images (Trivy):** fails on fixable CRITICALs; full HIGH/CRITICAL reported and
  archived as SBOM.

## Engineering defects found during P5

P5 gates caught real issues rather than merely documenting policy:

- `pip-audit` and `npm audit` found actionable Python and npm advisories; the
  runtime locks were upgraded before those gates became required.
- Trivy found two fixable CRITICAL OpenSSL findings (CVE-2026-31789 in
  `libcrypto3`/`libssl3`) in the API image. Runtime stages now take patched base
  OS packages before the image can be pushed.
- `CapabilityProbe` initially returned a cached optimistic value without
  executing its capability check on the first request. A regression test now
  proves the first call performs the real readiness check.
- Terraform refresh required the plan identity to call Container Apps/Jobs
  `listSecrets`; a narrowly scoped custom role fixed the 403 without granting a
  general write role.
- GitHub actually emitted immutable owner/repository-ID OIDC subjects even
  though the customization API flag suggested classic subjects. The federated
  credentials use the empirically observed immutable subject.
- `latest_revision_fqdn` is a revision hostname, not the stable Container App
  endpoint. The first smoke used that misleading output and failed; smoke and
  Terraform outputs now distinguish stable ingress from revision-specific FQDNs.

## Accepted security debt

- Trivy `AZU-0012` is an accepted staging networking tradeoff: AAD-only storage
  has shared keys disabled, while denying the public endpoint would currently
  break the Container Apps data plane and CI Terraform-state access.
- The plan identity can read Container App/Job secret values because provider
  refresh requires `listSecrets`, but only the two narrow custom actions are
  added to its otherwise read-only resource access.
- The database bootstrap password remains in the Terraform state and Container
  Apps secret path. Removing that bootstrap secret requires a separate database
  identity/design change.
- This public repository's Azure OIDC trust is constrained by environment-
  specific immutable subjects and `main`-only deployment policies. PR workflows
  receive no `id-token: write` and cannot authenticate to Azure.
- `staging` retains a required reviewer, but single-maintainer operation requires
  self-review to remain allowed until a second trusted reviewer exists.

## Cost controls

No GPU, no full-match run, no scaling changes. PostgreSQL should be stopped when
idle; the deploy workflow refuses to auto-start cost-bearing infrastructure.

## Known limitations

- Container Apps console logs did not reach Log Analytics in P4 — deferred to
  **P6 (observability)**.
- The frontend still displays "Local mode — No cloud services connected" in
  cloud staging. This cosmetic state is deferred to **P8**.
- Dependabot may not detect the non-standard `*.Dockerfile` names under
  `/docker`; base-image bumps can be applied manually if so.
