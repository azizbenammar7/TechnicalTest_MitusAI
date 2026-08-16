# FootballAI Azure staging Terraform

This root describes one low-cost Azure for Students staging environment. P3
uses local state and leaves `deploy_workloads = false`; application Container
Apps and the event-driven worker Job are represented but intentionally deferred
until immutable images have been pushed during P4.

The AzureRM provider has `resource_provider_registrations = "none"`. Terraform
must never register subscription providers implicitly. Register only the
reviewed providers as a separate operator action before the first apply.

```bash
cp terraform.tfvars.example terraform.tfvars
terraform init
terraform fmt -check -recursive
terraform validate
terraform plan -out=staging.tfplan
```

Never commit local variable files, plans, `.terraform/`, or state. The generated
PostgreSQL bootstrap password is sensitive and exists in state; protect local
state and migrate it to the documented Azure Storage backend before shared use.
