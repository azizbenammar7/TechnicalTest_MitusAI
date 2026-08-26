resource "azurerm_monitor_metric_alert" "servicebus_dead_letter" {
  name                = "alert-${local.name_prefix}-servicebus-dead-letter"
  resource_group_name = azurerm_resource_group.staging.name
  scopes              = [azurerm_servicebus_namespace.staging.id]
  description         = "Analysis messages are dead-lettered. Check queue reason, worker failures, and message contract before replaying."
  severity            = 1
  frequency           = "PT5M"
  window_size         = "PT5M"
  auto_mitigate       = true

  criteria {
    metric_namespace = "Microsoft.ServiceBus/namespaces"
    metric_name      = "DeadletteredMessages"
    aggregation      = "Maximum"
    operator         = "GreaterThan"
    threshold        = 0

    dimension {
      name     = "EntityName"
      operator = "Include"
      values   = [azurerm_servicebus_queue.analysis.name]
    }
  }

  tags = var.tags
}

resource "azurerm_monitor_metric_alert" "servicebus_queue_backlog" {
  name                = "alert-${local.name_prefix}-servicebus-backlog"
  resource_group_name = azurerm_resource_group.staging.name
  scopes              = [azurerm_servicebus_namespace.staging.id]
  description         = "More than ten analyses are waiting. Check worker executions, scaling, PostgreSQL state, and poison messages."
  severity            = 2
  frequency           = "PT5M"
  window_size         = "PT15M"
  auto_mitigate       = true

  criteria {
    metric_namespace = "Microsoft.ServiceBus/namespaces"
    metric_name      = "ActiveMessages"
    aggregation      = "Maximum"
    operator         = "GreaterThan"
    threshold        = 10

    dimension {
      name     = "EntityName"
      operator = "Include"
      values   = [azurerm_servicebus_queue.analysis.name]
    }
  }

  tags = var.tags
}

resource "azurerm_monitor_metric_alert" "api_server_errors" {
  count = var.deploy_workloads ? 1 : 0

  name                = "alert-${local.name_prefix}-api-5xx"
  resource_group_name = azurerm_resource_group.staging.name
  scopes              = [azurerm_container_app.api[0].id]
  description         = "The API returned repeated 5xx responses. Check readiness, dependencies, recent revisions, traces, and correlated errors."
  severity            = 1
  frequency           = "PT5M"
  window_size         = "PT5M"
  auto_mitigate       = true

  criteria {
    metric_namespace = "Microsoft.App/containerApps"
    metric_name      = "Requests"
    aggregation      = "Total"
    operator         = "GreaterThan"
    threshold        = 2

    dimension {
      name     = "statusCodeCategory"
      operator = "Include"
      values   = ["5xx"]
    }
  }

  tags = var.tags
}

resource "azurerm_monitor_metric_alert" "worker_job_failure" {
  count = var.deploy_workloads ? 1 : 0

  name                = "alert-${local.name_prefix}-worker-job-failure"
  resource_group_name = azurerm_resource_group.staging.name
  scopes              = [azurerm_container_app_job.worker[0].id]
  description         = "A worker execution failed. Correlate execution name and run_id, then check PostgreSQL, Service Bus, Blob, and stage errors."
  severity            = 1
  frequency           = "PT5M"
  window_size         = "PT5M"
  auto_mitigate       = true

  criteria {
    metric_namespace = "Microsoft.App/jobs"
    metric_name      = "Executions"
    aggregation      = "Total"
    operator         = "GreaterThan"
    threshold        = 0

    dimension {
      name     = "state"
      operator = "Include"
      values   = ["Failed"]
    }
  }

  tags = var.tags
}

resource "azurerm_monitor_metric_alert" "migration_job_failure" {
  count = var.deploy_workloads ? 1 : 0

  name                = "alert-${local.name_prefix}-migration-job-failure"
  resource_group_name = azurerm_resource_group.staging.name
  scopes              = [azurerm_container_app_job.migration[0].id]
  description         = "A schema migration failed. Stop deployment, inspect its trace/log, and verify PostgreSQL readiness before retrying."
  severity            = 0
  frequency           = "PT5M"
  window_size         = "PT5M"
  auto_mitigate       = true

  criteria {
    metric_namespace = "Microsoft.App/jobs"
    metric_name      = "Executions"
    aggregation      = "Total"
    operator         = "GreaterThan"
    threshold        = 0

    dimension {
      name     = "state"
      operator = "Include"
      values   = ["Failed"]
    }
  }

  tags = var.tags
}
