# Terraform state bootstrap

This root owns only the Azure resources required to store FootballAI staging
Terraform state. It is intentionally separate from
`environments/staging`, so destroying the staging root cannot destroy its own
backend.

The bootstrap uses local state for its one-time creation. Treat that local
state as sensitive: it is ignored by Git, must not be printed or committed,
and should remain readable only by its operator. The staging backend uses the
current Azure CLI identity with Microsoft Entra authorization; no Storage
Account key, SAS, or token belongs in configuration.

```bash
export TF_VAR_subscription_id="$(az account show --query id -o tsv)"
terraform init
terraform fmt -check
terraform validate
terraform plan -out=bootstrap.tfplan
terraform apply
```

Never apply this root as part of the FootballAI staging lifecycle.
