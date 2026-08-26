"""Deterministic tests for the TEMPORARY P6 one-time apply plan policy.

Exercises the exact P6 allowlist, action matching, destroy/replace rejection,
count enforcement, and sanitized-plan hash equivalence. Remove alongside
`infra/terraform/scripts/p6_plan_policy.py` after P6 staging validation.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "infra" / "terraform" / "scripts" / "p6_plan_policy.py"

# The exact reviewed P6 plan, as `terraform show -json` reports resource_changes
# (only the fields the policy reads: address + change.actions). Nine creates,
# five updates, matching the reviewed 9/5/0/0 plan on source SHA 43973346.
APPROVED_CHANGES = [
    ("azurerm_application_insights.staging", ["create"]),
    ("azurerm_monitor_diagnostic_setting.container_app_environment", ["create"]),
    ("azurerm_monitor_metric_alert.api_server_errors[0]", ["create"]),
    ("azurerm_monitor_metric_alert.migration_job_failure[0]", ["create"]),
    ("azurerm_monitor_metric_alert.servicebus_dead_letter", ["create"]),
    ("azurerm_monitor_metric_alert.servicebus_queue_backlog", ["create"]),
    ("azurerm_monitor_metric_alert.worker_job_failure[0]", ["create"]),
    ("azurerm_role_assignment.application_insights_api", ["create"]),
    ("azurerm_role_assignment.application_insights_worker", ["create"]),
    ("azurerm_container_app_environment.staging", ["update"]),
    ("azurerm_container_app.api[0]", ["update"]),
    ("azurerm_container_app.frontend[0]", ["update"]),
    ("azurerm_container_app_job.migration[0]", ["update"]),
    ("azurerm_container_app_job.worker[0]", ["update"]),
]

# Independently pinned expectation (must equal the reviewed hash baked into the
# policy) so a change to either side is caught here rather than silently passing.
EXPECTED_HASH = "3f3de0cf0a15fd7f8f457c659a1a6bba8879274a6c34f802b7ab261ca325c49d"


def _load_policy() -> ModuleType:
    spec = importlib.util.spec_from_file_location("p6_plan_policy", POLICY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = _load_policy()


def _plan(changes) -> dict:
    """Build a minimal `terraform show -json`-shaped plan from (address, actions)."""
    return {
        "resource_changes": [
            {"address": addr, "change": {"actions": list(actions)}}
            for addr, actions in changes
        ]
    }


def _approved_plan() -> dict:
    return _plan(APPROVED_CHANGES)


# --- reviewed hash is stable and matches the policy constant ------------------


def test_reviewed_hash_constant_matches_expected():
    assert policy.REVIEWED_SHA256 == EXPECTED_HASH


def test_approved_plan_hashes_to_reviewed_hash():
    result = policy.evaluate(_approved_plan())
    assert result["summary_hash"] == EXPECTED_HASH


# --- 1. exact approved plan -> PASS -------------------------------------------


def test_exact_approved_plan_passes():
    result = policy.evaluate(_approved_plan())
    assert result["ok"], result["violations"]
    assert result["violations"] == []
    assert result["counts"] == {"add": 9, "change": 5, "destroy": 0, "replace": 0}


def test_no_op_and_read_changes_are_ignored():
    changes = APPROVED_CHANGES + [
        ("azurerm_resource_group.staging", ["no-op"]),
        ("data.azurerm_client_config.current", ["read"]),
    ]
    result = policy.evaluate(_plan(changes))
    assert result["ok"], result["violations"]
    assert result["summary_hash"] == EXPECTED_HASH


# --- 2. unexpected create -> FAIL ---------------------------------------------


def test_unexpected_create_fails():
    changes = APPROVED_CHANGES + [("azurerm_storage_account.rogue", ["create"])]
    result = policy.evaluate(_plan(changes))
    assert not result["ok"]
    assert any("not in P6 allowlist" in v for v in result["violations"])


# --- 3. unexpected update -> FAIL ---------------------------------------------


def test_unexpected_update_fails():
    changes = APPROVED_CHANGES + [("azurerm_key_vault.rogue", ["update"])]
    result = policy.evaluate(_plan(changes))
    assert not result["ok"]
    assert any("not in P6 allowlist" in v for v in result["violations"])


# --- 4. delete -> FAIL --------------------------------------------------------


def test_delete_fails():
    changes = copy.deepcopy(APPROVED_CHANGES)
    changes.append(("azurerm_application_insights.staging_old", ["delete"]))
    result = policy.evaluate(_plan(changes))
    assert not result["ok"]
    assert any(v.startswith("DESTROY not allowed") for v in result["violations"])


# --- 5. replacement -> FAIL ---------------------------------------------------


def test_replacement_fails():
    # An allowlisted address, but replaced (delete+create) rather than updated.
    changes = [
        (addr, ["delete", "create"]) if addr == "azurerm_container_app.api[0]" else (addr, acts)
        for addr, acts in APPROVED_CHANGES
    ]
    result = policy.evaluate(_plan(changes))
    assert not result["ok"]
    assert any(v.startswith("REPLACE not allowed") for v in result["violations"])


# --- 6. count mismatch -> FAIL ------------------------------------------------


def test_missing_resource_count_mismatch_fails():
    changes = [c for c in APPROVED_CHANGES if c[0] != "azurerm_role_assignment.application_insights_worker"]
    result = policy.evaluate(_plan(changes))
    assert not result["ok"]
    assert any("missing from plan" in v for v in result["violations"])
    assert any("totals mismatch" in v for v in result["violations"])
    # Hash gate also trips because the sanitized set differs.
    assert any("hash mismatch" in v for v in result["violations"])


def test_duplicate_allowlisted_resource_fails():
    changes = APPROVED_CHANGES + [("azurerm_container_app.api[1]", ["update"])]
    result = policy.evaluate(_plan(changes))
    assert not result["ok"]
    assert any("duplicate change" in v for v in result["violations"])


# --- 7. correct resources but wrong action -> FAIL ----------------------------


def test_correct_resource_wrong_action_fails():
    # Application Insights reviewed as create; a plan that updates it must fail.
    changes = [
        (addr, ["update"]) if addr == "azurerm_application_insights.staging" else (addr, acts)
        for addr, acts in APPROVED_CHANGES
    ]
    result = policy.evaluate(_plan(changes))
    assert not result["ok"]
    assert any("unexpected action" in v for v in result["violations"])


# --- 8. canonical hash mismatch -> FAIL ---------------------------------------


def test_hash_gate_mismatch_fails():
    # Structurally valid plan, but the required hash differs -> refuse.
    wrong = "0" * 64
    result = policy.evaluate(_approved_plan(), require_hash=wrong)
    assert not result["ok"]
    assert any("hash mismatch" in v for v in result["violations"])


def test_hash_gate_can_be_disabled_for_rule_isolation():
    result = policy.evaluate(_approved_plan(), require_hash=None)
    assert result["ok"], result["violations"]


# --- CLI: exit codes and sanitized output (no attribute values) ---------------


def _run_cli(plan: dict, *extra: str):
    return subprocess.run(
        [sys.executable, str(POLICY_PATH), "/dev/stdin", *extra],
        input=json.dumps(plan),
        capture_output=True,
        text=True,
    )


def test_cli_exit_zero_on_approved_plan():
    proc = _run_cli(_approved_plan())
    assert proc.returncode == 0, proc.stderr
    assert f"summary_sha256={EXPECTED_HASH}" in proc.stdout
    assert "PASS" in proc.stdout


def test_cli_exit_two_on_violation():
    changes = APPROVED_CHANGES + [("azurerm_storage_account.rogue", ["create"])]
    proc = _run_cli(_plan(changes))
    assert proc.returncode == 2
    assert "P6 POLICY VIOLATIONS" in proc.stderr


def test_cli_output_is_sanitized_addresses_and_actions_only():
    # Feed a plan whose before/after carry a secret; it must never be emitted.
    plan = _approved_plan()
    plan["resource_changes"][0]["change"]["after"] = {"connection_string": "SUPER_SECRET_VALUE"}
    proc = _run_cli(plan)
    assert proc.returncode == 0, proc.stderr
    assert "SUPER_SECRET_VALUE" not in proc.stdout
    assert "SUPER_SECRET_VALUE" not in proc.stderr


def test_cli_bad_json_is_usage_error():
    proc = subprocess.run(
        [sys.executable, str(POLICY_PATH)],
        input="not json",
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
