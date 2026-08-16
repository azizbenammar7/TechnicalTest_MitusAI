# FootballAI Azure staging Terraform

This root describes one low-cost Azure for Students staging environment. It
stores state in the isolated `footballaitfstg1a06f8` account under
`footballai/staging.tfstate` using the active Azure CLI identity and Microsoft
Entra authorization. Application Container Apps and the event-driven worker Job
are represented but remain deferred until immutable images are pushed during a
later controlled phase.

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
PostgreSQL bootstrap password is sensitive and exists only in protected remote
state after apply. Never configure the backend with a Storage Account key, SAS,
or other static credential.
