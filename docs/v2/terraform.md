# Terraform operations

The Azure staging root is `infra/terraform/environments/staging`. It is a single
environment root rather than a set of empty wrapper modules. Terraform 1.15.8
validated it with AzureRM 4.81.0 and Random 3.9.0 from the dependency lock file.

## Safe review workflow

```bash
cd infra/terraform/environments/staging
cp terraform.tfvars.example terraform.tfvars
# Set the selected subscription ID in the ignored local file.
terraform init
terraform fmt -check -recursive
terraform validate
terraform plan -refresh=false -out=staging.tfplan
```

P3's default plan keeps `deploy_workloads = false`: it creates the foundation
only. A separate review plan with `deploy_workloads = true` confirms the two
Container Apps and event-driven Job are syntactically represented, but must not
be applied until ACR contains immutable Git-SHA images and the first controlled
apply is approved.

The provider sets `resource_provider_registrations = "none"`. Required providers
must be reviewed and registered by an operator before apply; Terraform will not
silently mutate subscription registration state. Authentication comes from the
active Azure CLI session locally and should use GitHub OIDC in future CI.

## State and secrets

P3 uses local state only. `.terraform/`, `*.tfstate*`, `*.tfplan`, local `.tfvars`
and override files are ignored. A future bootstrap creates a dedicated,
restricted state resource group, Storage account and private container; then
state is migrated with the settings in `backend.hcl.example`. State must be
encrypted, access logged, RBAC restricted and locking enabled before team use.

Terraform generates the initial PostgreSQL password. It is marked sensitive but
would still exist in state, as can provider-computed storage, Service Bus and Log
Analytics sensitive attributes even when key-based/local authentication is
disabled. Never commit or share state or saved plans. No secret is an output and
none belongs in `.tfvars`, source, logs, commits or CI variables. Entra database
authentication is the desired future replacement for the bootstrap password.

## First-apply gates

Before any apply:

1. review and explicitly register only required providers;
2. verify France Central quotas and current Azure for Students credit/costs;
3. review the saved plan for exactly the expected resource group;
4. bootstrap/protect remote state or accept the explicit single-operator local
   state risk;
5. keep `deploy_workloads=false` for the first foundation apply;
6. obtain explicit operator authorization.

Do not use `-auto-approve`. Do not apply a stale saved plan. The first apply is
outside P3.

## Workload and destroy strategy

P4 builds and pushes frontend, API and worker images with the same immutable Git
SHA, runs database migrations as an explicit release step, and then reviews a
new `deploy_workloads=true` plan. The worker's `FOOTBALLAI_WORKER_ONCE=1` mode
makes each Container Apps Job execution consume one Service Bus delivery.

Terraform owns the complete staging resource group. A reviewed destroy should
therefore remove the environment, including PostgreSQL and Blob data. Export any
valuable demonstration data first, confirm the target subscription and workspace,
and review a destroy plan separately. Never manually delete isolated managed
resources because that creates drift.
