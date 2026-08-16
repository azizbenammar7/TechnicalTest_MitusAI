locals {
  database_url = "postgresql+psycopg://footballai_admin:${random_password.postgres_admin.result}@${azurerm_postgresql_flexible_server.staging.fqdn}:5432/${azurerm_postgresql_flexible_server_database.footballai.name}?sslmode=require"

  common_cloud_environment = {
    FOOTBALLAI_ENVIRONMENT            = "staging"
    FOOTBALLAI_DATABASE_BACKEND       = "postgres"
    FOOTBALLAI_OBJECT_STORAGE_BACKEND = "azure_blob"
    FOOTBALLAI_QUEUE_BACKEND          = "azure_service_bus"
    FOOTBALLAI_BLOB_ACCOUNT_URL       = azurerm_storage_account.staging.primary_blob_endpoint
    FOOTBALLAI_BLOB_CONTAINER         = azurerm_storage_container.runs.name
    FOOTBALLAI_SERVICEBUS_NAMESPACE   = "${azurerm_servicebus_namespace.staging.name}.servicebus.windows.net"
    FOOTBALLAI_SERVICEBUS_QUEUE       = azurerm_servicebus_queue.analysis.name
    FOOTBALLAI_CODE_REVISION          = var.image_tag
    FOOTBALLAI_CODE_DIRTY             = "0"
    FOOTBALLAI_LOG_LEVEL              = "INFO"
  }
}

resource "azurerm_container_app" "api" {
  count = var.deploy_workloads ? 1 : 0

  name                         = "ca-${local.name_prefix}-api"
  container_app_environment_id = azurerm_container_app_environment.staging.id
  resource_group_name          = azurerm_resource_group.staging.name
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.api.id]
  }

  registry {
    server   = azurerm_container_registry.staging.login_server
    identity = azurerm_user_assigned_identity.api.id
  }

  secret {
    name  = "database-url"
    value = local.database_url
  }

  template {
    min_replicas = 0
    max_replicas = 1

    container {
      name   = "api"
      image  = "${azurerm_container_registry.staging.login_server}/footballai-api:${var.image_tag}"
      cpu    = 0.5
      memory = "1Gi"

      dynamic "env" {
        for_each = merge(local.common_cloud_environment, {
          AZURE_CLIENT_ID             = azurerm_user_assigned_identity.api.client_id
          FOOTBALLAI_API_WORKERS      = "1"
          FOOTBALLAI_V2_CORS_ORIGINS  = "https://same-origin.invalid"
          FOOTBALLAI_MAX_UPLOAD_BYTES = tostring(8 * 1024 * 1024 * 1024)
          FOOTBALLAI_DATABASE_URL     = null
        })
        content {
          name        = env.key
          value       = env.value
          secret_name = env.key == "FOOTBALLAI_DATABASE_URL" ? "database-url" : null
        }
      }

      liveness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/api/health"
      }

      readiness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/api/ready"
      }
    }
  }

  ingress {
    external_enabled           = false
    target_port                = 8000
    allow_insecure_connections = false

    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  depends_on = [azurerm_role_assignment.api]
  tags       = var.tags
}

resource "azurerm_container_app" "frontend" {
  count = var.deploy_workloads ? 1 : 0

  name                         = "ca-${local.name_prefix}-frontend"
  container_app_environment_id = azurerm_container_app_environment.staging.id
  resource_group_name          = azurerm_resource_group.staging.name
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.frontend.id]
  }

  registry {
    server   = azurerm_container_registry.staging.login_server
    identity = azurerm_user_assigned_identity.frontend.id
  }

  template {
    min_replicas = 0
    max_replicas = 1

    container {
      name   = "frontend"
      image  = "${azurerm_container_registry.staging.login_server}/footballai-frontend:${var.image_tag}"
      cpu    = 0.25
      memory = "0.5Gi"

      env {
        name  = "FOOTBALLAI_FRONTEND_API_BASE"
        value = ""
      }

      env {
        name  = "FOOTBALLAI_API_UPSTREAM"
        value = "https://${azurerm_container_app.api[0].latest_revision_fqdn}"
      }

      liveness_probe {
        transport = "HTTP"
        port      = 8080
        path      = "/healthz"
      }
    }
  }

  ingress {
    external_enabled           = true
    target_port                = 8080
    allow_insecure_connections = false

    traffic_weight {
      percentage      = 100
      latest_revision = true
    }
  }

  depends_on = [azurerm_role_assignment.frontend]
  tags       = var.tags
}

resource "azurerm_container_app_job" "worker" {
  count = var.deploy_workloads ? 1 : 0

  name                         = "caj-${local.name_prefix}-worker"
  location                     = azurerm_resource_group.staging.location
  resource_group_name          = azurerm_resource_group.staging.name
  container_app_environment_id = azurerm_container_app_environment.staging.id
  replica_timeout_in_seconds   = 7200
  replica_retry_limit          = 1

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.worker.id]
  }

  registry {
    server   = azurerm_container_registry.staging.login_server
    identity = azurerm_user_assigned_identity.worker.id
  }

  secret {
    name  = "database-url"
    value = local.database_url
  }

  event_trigger_config {
    parallelism              = 1
    replica_completion_count = 1

    scale {
      min_executions              = 0
      max_executions              = 1
      polling_interval_in_seconds = 30

      rules {
        name             = "servicebus-analysis-jobs"
        custom_rule_type = "azure-servicebus"
        identity_id      = azurerm_user_assigned_identity.worker.id
        metadata = {
          namespace    = azurerm_servicebus_namespace.staging.name
          queueName    = azurerm_servicebus_queue.analysis.name
          messageCount = "1"
        }
      }
    }
  }

  template {
    container {
      name   = "worker"
      image  = "${azurerm_container_registry.staging.login_server}/footballai-worker:${var.image_tag}"
      cpu    = 1.0
      memory = "2Gi"

      dynamic "env" {
        for_each = merge(local.common_cloud_environment, {
          AZURE_CLIENT_ID                     = azurerm_user_assigned_identity.worker.client_id
          FOOTBALLAI_DATABASE_URL             = null
          FOOTBALLAI_WORKER_ONCE              = "1"
          FOOTBALLAI_DEMO_STAGE_DELAY_SECONDS = "0.12"
        })
        content {
          name        = env.key
          value       = env.value
          secret_name = env.key == "FOOTBALLAI_DATABASE_URL" ? "database-url" : null
        }
      }
    }
  }

  depends_on = [azurerm_role_assignment.worker]
  tags       = var.tags
}
