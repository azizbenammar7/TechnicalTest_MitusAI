"""Historical Alembic revisions are deterministic on disposable PostgreSQL."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import create_engine, inspect  # noqa: E402


_DATABASE_URL = os.getenv("FOOTBALLAI_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DATABASE_URL,
    reason="set FOOTBALLAI_TEST_DATABASE_URL to test migration history",
)


def _config() -> Config:
    migrations = Path(__file__).resolve().parents[1] / "migrations"
    os.environ["FOOTBALLAI_DATABASE_URL"] = str(_DATABASE_URL)
    config = Config()
    config.set_main_option("script_location", str(migrations))
    return config


def test_empty_to_head_and_0001_to_0002_are_deterministic():
    config = _config()
    engine = create_engine(_DATABASE_URL, future=True)
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "0001_initial")
        columns_0001 = {
            item["name"] for item in inspect(engine).get_columns("analysis_attempts")
        }
        assert "cancel_requested" not in columns_0001

        command.upgrade(config, "head")
        columns_head = {
            item["name"] for item in inspect(engine).get_columns("analysis_attempts")
        }
        assert "cancel_requested" in columns_head

        command.downgrade(config, "0001_initial")
        columns_downgraded = {
            item["name"] for item in inspect(engine).get_columns("analysis_attempts")
        }
        assert "cancel_requested" not in columns_downgraded

        command.downgrade(config, "base")
        assert "analysis_attempts" not in inspect(engine).get_table_names()
        command.upgrade(config, "head")
        assert "cancel_requested" in {
            item["name"] for item in inspect(engine).get_columns("analysis_attempts")
        }
    finally:
        engine.dispose()
