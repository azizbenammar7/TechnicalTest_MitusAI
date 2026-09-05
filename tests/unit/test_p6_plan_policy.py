"""Deterministic tests for the TEMPORARY P6 one-time apply plan policy.

Exercises the exact P6 RECOVERY allowlist, action matching, destroy/replace
rejection, count enforcement, and sanitized-plan hash equivalence. Remove
alongside `infra/terraform/scripts/p6_plan_policy.py` after P6 staging
validation.

Contract history
----------------
The ORIGINAL reviewed pre-apply plan was 9 add / 5 change / 0 destroy /
0 replace, hash 3f3de0cf...  That apply partially succeeded: it converged the
Container Apps Environment logging destination (azure-monitor) before the
Application-Insights-backed resources failed. The environment is therefore
already converged and its in-place update must NOT reappear.

The authoritative POST-PARTIAL-APPLY RECOVERY plan is the original plan minus
exactly that one `azurerm_container_app_environment.staging` update: 9 add /
4 change / 0 destroy / 0 replace, hash 788aaab8...  Both hashes are pinned
below so a change to either the policy or the plan is caught here rather than
silently passing.
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

# The exact RECOVERY plan, as `terraform show -json` reports resource_changes
# (only the fields the policy reads: address + change.actions). Nine creates,
# four updates: the reviewed 9/4/0/0 recovery plan on the P6 release. The
# Container Apps Environment update is intentionally ABSENT (already converged).
RECOVERY_CHANGES = [
    ("azurerm_application_insights.staging", ["create"]),
    ("azurerm_monitor_diagnostic_setting.container_app_environment", ["create"]),
    ("azurerm_monitor_metric_alert.api_server_errors[0]", ["create"]),
    ("azurerm_monitor_metric_alert.migration_job_failure[0]", ["create"]),
    ("azurerm_monitor_metric_alert.servicebus_dead_letter", ["create"]),
    ("azurerm_monitor_metric_alert.servicebus_queue_backlog", ["create"]),
    ("azurerm_monitor_metric_alert.worker_job_failure[0]", ["create"]),
    ("azurerm_role_assignment.application_insights_api", ["create"]),
    ("azurerm_role_assignment.application_insights_worker", ["create"]),
    ("azurerm_container_app.api[0]", ["update"]),
    ("azurerm_container_app.frontend[0]", ["update"]),
    ("azurerm_container_app_job.migration[0]", ["update"]),
    ("azurerm_container_app_job.worker[0]", ["update"]),
]

# The ORIGINAL pre-apply plan (historical): the recovery plan PLUS the one
# Container Apps Environment update that has since converged. Kept so the test
# suite proves the recovery policy now REJECTS the superseded 14-action plan.
ORIGINAL_PRE_APPLY_CHANGES = RECOVERY_CHANGES + [
    ("azurerm_container_app_environment.staging", ["update"]),
]

# Independently pinned expectations (must equal the constants baked into the
# policy) so a change to either side is caught here rather than silently passing.
RECOVERY_HASH = "788aaab8c160b14daa12384b6f75159c42e471f1e3adad750b17de3aa48451ee"
# Historical only — the recovery policy must reject this.
ORIGINAL_PRE_APPLY_HASH = "3f3de0cf0a15fd7f8f457c659a1a6bba8879274a6c34f802b7ab261ca325c49d"


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


def _recovery_plan() -> dict:
    return _plan(RECOVERY_CHANGES)


def _original_pre_apply_plan() -> dict:
    return _plan(ORIGINAL_PRE_APPLY_CHANGES)


# --- recovery hash is stable and matches the policy constant ------------------


def test_recovery_hash_constant_matches_expected():
    assert policy.REVIEWED_SHA256 == RECOVERY_HASH


def test_recovery_plan_hashes_to_recovery_hash():
    result = policy.evaluate(_recovery_plan())
    assert result["summary_hash"] == RECOVERY_HASH


def test_policy_no_longer_pins_the_original_pre_apply_hash():
    # Guards against a copy-paste regression that reintroduces the old contract.
    assert policy.REVIEWED_SHA256 != ORIGINAL_PRE_APPLY_HASH


# --- 1. exact recovery plan -> PASS -------------------------------------------


def test_exact_recovery_plan_passes():
    result = policy.evaluate(_recovery_plan())
    assert result["ok"], result["violations"]
    assert result["violations"] == []
    assert result["counts"] == {"add": 9, "change": 4, "destroy": 0, "replace": 0}


def test_no_op_and_read_changes_are_ignored():
    changes = RECOVERY_CHANGES + [
        ("azurerm_resource_group.staging", ["no-op"]),
        ("data.azurerm_client_config.current", ["read"]),
        # The already-converged environment now shows as a no-op, not an update.
        ("azurerm_container_app_environment.staging", ["no-op"]),
    ]
    result = policy.evaluate(_plan(changes))
    assert result["ok"], result["violations"]
    assert result["summary_hash"] == RECOVERY_HASH


# --- 2. original 14-action pre-apply plan -> FAIL -----------------------------


def test_original_pre_apply_plan_is_rejected():
    # The environment update has converged; if it reappears the recovery policy
    # must refuse: it is no longer allowlisted, the totals are 9/5 not 9/4, and
    # the sanitized set no longer matches the recovery hash.
    result = policy.evaluate(_original_pre_apply_plan())
    assert not result["ok"]
    assert any(
        "not in P6 allowlist" in v
        and "azurerm_container_app_environment.staging" in v
        for v in result["violations"]
    )
    assert any("totals mismatch" in v for v in result["violations"])
    assert any("hash mismatch" in v for v in result["violations"])


# --- 3. unexpected create -> FAIL ---------------------------------------------


def test_unexpected_create_fails():
    changes = RECOVERY_CHANGES + [("azurerm_storage_account.rogue", ["create"])]
    result = policy.evaluate(_plan(changes))
    assert not result["ok"]
    assert any("not in P6 allowlist" in v for v in result["violations"])


# --- 4. unexpected update -> FAIL ---------------------------------------------


def test_unexpected_update_fails():
    changes = RECOVERY_CHANGES + [("azurerm_key_vault.rogue", ["update"])]
    result = policy.evaluate(_plan(changes))
    assert not result["ok"]
    assert any("not in P6 allowlist" in v for v in result["violations"])


# --- 5. delete -> FAIL --------------------------------------------------------


def test_delete_fails():
    changes = copy.deepcopy(RECOVERY_CHANGES)
    changes.append(("azurerm_application_insights.staging_old", ["delete"]))
    result = policy.evaluate(_plan(changes))
    assert not result["ok"]
    assert any(v.startswith("DESTROY not allowed") for v in result["violations"])


# --- 6. replacement -> FAIL ---------------------------------------------------


def test_replacement_fails():
    # An allowlisted address, but replaced (delete+create) rather than updated.
    changes = [
        (addr, ["delete", "create"]) if addr == "azurerm_container_app.api[0]" else (addr, acts)
        for addr, acts in RECOVERY_CHANGES
    ]
    result = policy.evaluate(_plan(changes))
    assert not result["ok"]
    assert any(v.startswith("REPLACE not allowed") for v in result["violations"])


# --- 7. missing action / count mismatch -> FAIL -------------------------------


def test_missing_resource_count_mismatch_fails():
    changes = [c for c in RECOVERY_CHANGES if c[0] != "azurerm_role_assignment.application_insights_worker"]
    result = policy.evaluate(_plan(changes))
    assert not result["ok"]
    assert any("missing from plan" in v for v in result["violations"])
    assert any("totals mismatch" in v for v in result["violations"])
    # Hash gate also trips because the sanitized set differs.
    assert any("hash mismatch" in v for v in result["violations"])


# --- 8. duplicate action -> FAIL ----------------------------------------------


def test_duplicate_allowlisted_resource_fails():
    changes = RECOVERY_CHANGES + [("azurerm_container_app.api[1]", ["update"])]
    result = policy.evaluate(_plan(changes))
    assert not result["ok"]
    assert any("duplicate change" in v for v in result["violations"])


# --- 9. correct resources but wrong action -> FAIL ----------------------------


def test_correct_resource_wrong_action_fails():
    # Application Insights reviewed as create; a plan that updates it must fail.
    changes = [
        (addr, ["update"]) if addr == "azurerm_application_insights.staging" else (addr, acts)
        for addr, acts in RECOVERY_CHANGES
    ]
    result = policy.evaluate(_plan(changes))
    assert not result["ok"]
    assert any("unexpected action" in v for v in result["violations"])


# --- 10. Add/Change count mismatch -> FAIL ------------------------------------


def test_add_change_count_mismatch_fails():
    # Turn one reviewed create into an update: still 13 addresses, but 8/5 not
    # 9/4. The action-match rule and the totals rule both trip.
    changes = [
        (addr, ["update"]) if addr == "azurerm_role_assignment.application_insights_api" else (addr, acts)
        for addr, acts in RECOVERY_CHANGES
    ]
    result = policy.evaluate(_plan(changes))
    assert not result["ok"]
    assert any("totals mismatch" in v for v in result["violations"])


# --- 11. old pre-apply hash -> FAIL -------------------------------------------


def test_old_pre_apply_hash_is_rejected():
    # Even the exact recovery plan must be refused if the required hash is the
    # superseded pre-apply hash.
    result = policy.evaluate(_recovery_plan(), require_hash=ORIGINAL_PRE_APPLY_HASH)
    assert not result["ok"]
    assert any("hash mismatch" in v for v in result["violations"])


# --- 12. new recovery hash -> PASS (covered by constant + plan hash above) -----


def test_recovery_hash_gate_passes_when_required_explicitly():
    result = policy.evaluate(_recovery_plan(), require_hash=RECOVERY_HASH)
    assert result["ok"], result["violations"]


# --- canonical hash mismatch (arbitrary) -> FAIL ------------------------------


def test_hash_gate_mismatch_fails():
    # Structurally valid plan, but the required hash differs -> refuse.
    wrong = "0" * 64
    result = policy.evaluate(_recovery_plan(), require_hash=wrong)
    assert not result["ok"]
    assert any("hash mismatch" in v for v in result["violations"])


def test_hash_gate_can_be_disabled_for_rule_isolation():
    result = policy.evaluate(_recovery_plan(), require_hash=None)
    assert result["ok"], result["violations"]


# --- CLI: exit codes and sanitized output (no attribute values) ---------------


def _run_cli(plan: dict, *extra: str):
    return subprocess.run(
        [sys.executable, str(POLICY_PATH), "/dev/stdin", *extra],
        input=json.dumps(plan),
        capture_output=True,
        text=True,
    )


def test_cli_exit_zero_on_recovery_plan():
    proc = _run_cli(_recovery_plan())
    assert proc.returncode == 0, proc.stderr
    assert f"summary_sha256={RECOVERY_HASH}" in proc.stdout
    assert "PASS" in proc.stdout


def test_cli_exit_two_on_violation():
    changes = RECOVERY_CHANGES + [("azurerm_storage_account.rogue", ["create"])]
    proc = _run_cli(_plan(changes))
    assert proc.returncode == 2
    assert "P6 POLICY VIOLATIONS" in proc.stderr


# --- 13. secret values from plan JSON are never emitted -----------------------


def test_cli_output_is_sanitized_addresses_and_actions_only():
    # Feed a plan whose before/after carry a secret; it must never be emitted.
    plan = _recovery_plan()
    plan["resource_changes"][0]["change"]["after"] = {"connection_string": "SUPER_SECRET_VALUE"}
    proc = _run_cli(plan)
    assert proc.returncode == 0, proc.stderr
    assert "SUPER_SECRET_VALUE" not in proc.stdout
    assert "SUPER_SECRET_VALUE" not in proc.stderr


# --- 14. CLI exit behavior remains deterministic ------------------------------


def test_cli_bad_json_is_usage_error():
    proc = subprocess.run(
        [sys.executable, str(POLICY_PATH)],
        input="not json",
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1


def test_cli_original_pre_apply_plan_exits_two():
    # The superseded 14-action plan must be a hard policy violation at the CLI.
    proc = _run_cli(_original_pre_apply_plan())
    assert proc.returncode == 2
    assert "P6 POLICY VIOLATIONS" in proc.stderr
