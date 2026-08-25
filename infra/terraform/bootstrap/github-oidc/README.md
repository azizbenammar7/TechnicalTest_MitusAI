# GitHub Actions → Azure OIDC bootstrap

Secretless federation between GitHub Actions and Azure for the FootballAI
staging environment. Creates three least-privilege User-Assigned Managed
Identities (UAMIs), each trusted by exactly one GitHub Actions **environment**.
No App Registration, no client secret, no `AZURE_CREDENTIALS` is ever produced.

## Why UAMIs (not an App Registration)

A UAMI + federated credential is a pure ARM resource — creating it needs only
subscription-level rights, not Entra directory-admin rights the operator may not
hold in the ESPRIT tenant. A UAMI can never hold a password, so the "no secret"
guarantee is structural.

## Identities and scopes

| Identity | GitHub environment | Azure role → scope |
|---|---|---|
| `id-footballai-gha-build`  | `staging-build` | `AcrPush` → ACR `footballaistg1a06f8` |
| `id-footballai-gha-plan`   | `staging-plan`  | `Reader` + Container Apps/Jobs `listSecrets` custom actions → `rg-footballai-stg`; `Storage Blob Data Contributor` → `tfstate` container; `AcrPull` → ACR |
| `id-footballai-gha-deploy` | `staging`       | `Contributor` → `rg-footballai-stg`; `Storage Blob Data Contributor` → `tfstate` container; `AcrPull` → ACR |

`Contributor` deliberately **excludes** `Microsoft.Authorization/roleAssignments/write`.
A normal image-tag deploy leaves `rbac.tf` role assignments unchanged (no-op),
so this is sufficient. If a plan ever shows an RBAC change, apply fails on that
write — the intended **STOP** signal, never a reason to grant Owner / User
Access Administrator.

The plan identity's custom role contains only
`Microsoft.App/containerApps/listSecrets/action` and
`Microsoft.App/jobs/listSecrets/action`. AzureRM provider refresh invokes these
for workloads with a Terraform `secret` block; the role grants no mutation,
delete or IAM action.

## OIDC subject format

**Immutable** (`repo:OWNER@<owner_id>/REPO@<repo_id>:environment:<name>`).

Verified *empirically* from a real Actions run on 2026-08-24 — the emitted token
`sub` is:

```
repo:azizbenammar7@126194752/FootballAi@1264402679:environment:<name>
```

The `gh api .../actions/oidc/customization/sub` endpoint reported
`use_immutable_subject=false`, but that flag was **not** reliable for this repo
(created after GitHub's immutable-subject rollout); the `sub_claim_prefix` it
returned was the truth. Federated credentials were initially created with the
classic subject, the login failed with `AADSTS700213: No matching federated
identity record`, and the subjects were corrected to the immutable form. If
GitHub ever reverts this repo to classic subjects, switch `main.tf`'s
`subject_prefix` back to `repo:${var.github_owner}/${var.github_repo}:environment:`.

## State isolation

Separate backend key `footballai/github-oidc.tfstate` in the existing secure
state Storage Account — never mixed with `footballai/staging.tfstate`.

## Operator runbook (one-time, human-run — never CI)

```bash
az account set --subscription 9a86953f-5652-4d79-b405-08a895792f53
az account show   # confirm user = Aziz.BenAmmar@esprit.tn, tenant 604f1a96...

cd infra/terraform/bootstrap/github-oidc
cp terraform.tfvars.example terraform.tfvars   # then fill subscription_id
terraform init
terraform plan -out=oidc.tfplan
terraform apply oidc.tfplan
```

Creating the role assignments requires the operator to hold Owner (or User
Access Administrator) on the target scopes — a one-time human bootstrap. CI
never runs this; it only *uses* the resulting identities. The `*_client_id`
outputs are loaded into GitHub as environment variables (non-secret).
