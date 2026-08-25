"""Observable, fail-closed Alembic entry point for the Container Apps job."""

from __future__ import annotations

import logging
import os
import time

from alembic import command
from alembic.config import Config

from footballai_v2.logging_config import bind_log_context, log_event
from footballai_v2.observability import add_metric, configure_observability, record_metric, span


def main() -> None:
    configure_observability("footballai-migration")
    logger = logging.getLogger("footballai_v2.migration")
    environment = os.getenv("FOOTBALLAI_ENVIRONMENT", "local")
    execution_name = os.getenv("CONTAINER_APP_JOB_EXECUTION_NAME")
    started = time.perf_counter()
    with bind_log_context(
        job_execution_id=execution_name,
        code_revision=os.getenv("FOOTBALLAI_CODE_REVISION"),
    ):
        log_event(logger, logging.INFO, "migration.started", "Database migration started", status="running")
        try:
            with span("database.migration", environment=environment):
                command.upgrade(Config(os.getenv("FOOTBALLAI_ALEMBIC_CONFIG", "/opt/footballai/alembic.ini")), "head")
        except Exception as exc:
            elapsed = time.perf_counter() - started
            log_event(logger, logging.ERROR, "migration.failed", "Database migration failed", status="failed", duration_ms=round(elapsed * 1000, 2), error_type=type(exc).__name__, error_code="migration_failed", exc_info=True)
            add_metric("migration_job_failure", service="migration", environment=environment, status="failed")
            record_metric("migration_duration_seconds", elapsed, unit="s", service="migration", environment=environment, status="failed")
            raise
        elapsed = time.perf_counter() - started
        log_event(logger, logging.INFO, "migration.completed", "Database migration completed", status="succeeded", duration_ms=round(elapsed * 1000, 2))
        add_metric("migration_job_success", service="migration", environment=environment, status="succeeded")
        record_metric("migration_duration_seconds", elapsed, unit="s", service="migration", environment=environment, status="succeeded")


if __name__ == "__main__":
    main()
