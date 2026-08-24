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
| `id-footballai-gha-plan`   | `staging-plan`  | `Reader` → `rg-footballai-stg`; `Storage Blob Data Contributor` → `tfstate` container; `AcrPull` → ACR |
| `id-footballai-gha-deploy` | `staging`       | `Contributor` → `rg-footballai-stg`; `Storage Blob Data Contributor` → `tfstate` container; `AcrPull` → ACR |

`Contributor` deliberately **excludes** `Microsoft.Authorization/roleAssignments/write`.
A normal image-tag deploy leaves `rbac.tf` role assignments unchanged (no-op),
so this is sufficient. If a plan ever shows an RBAC change, apply fails on that
write — the intended **STOP** signal, never a reason to grant Owner / User
Access Administrator.

## OIDC subject format

Classic. Verified on this repository via
`gh api repos/azizbenammar7/FootballAi/actions/oidc/customization/sub`
(`use_immutable_subject=false`, `use_default=true`), so the emitted token
`sub` is `repo:azizbenammar7/FootballAi:environment:<name>`. If the repository is
later switched to immutable subjects, update `main.tf`'s `subject_prefix` to the
`repo:azizbenammar7@126194752/FootballAi@1264402679:environment:<name>` form.

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
