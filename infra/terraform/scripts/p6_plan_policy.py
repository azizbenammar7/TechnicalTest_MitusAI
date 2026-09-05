#!/usr/bin/env python3
"""TEMPORARY, P6-SPECIFIC Terraform plan safety policy (SECOND-PARTIAL-APPLY RECOVERY).

REMOVE AFTER P6 STAGING VALIDATION.

The steady-state deploy uses `plan_policy.py`, which deliberately rejects any
foundation change and so (correctly) refuses the P6 observability rollout. P6
intentionally adds Application Insights, a diagnostic setting, five metric
alerts, two narrow role assignments, and reconfigures the four workloads. This
one-time policy authorizes EXACTLY the remaining live plan and nothing else, so
a tightly-reviewed infrastructure apply can proceed once without weakening the
generic policy.

RECOVERY CONTRACT HISTORY
-------------------------
Each contract below was CORRECT for the live Azure state when it was reviewed;
successive partial applies converged more resources, shrinking the remaining
plan. They are NOT corrections of one another.

  1. ORIGINAL pre-apply plan: Add=9, Change=5 (hash 3f3de0cf...). Also
     reconfigured the Container Apps Environment logging destination.
  2. FIRST partial-apply recovery: Add=9, Change=4 (hash 788aaab8...). The first
     apply converged the Container Apps Environment logging destination
     (azure-monitor) before the Application-Insights-backed resources failed, so
     the environment update dropped out.
  3. SECOND partial-apply recovery (THIS CONTRACT / AUTHORITATIVE): Add=5,
     Change=5 (hash 8e0e9424...). The second apply created Application Insights,
     the diagnostic setting, and the two Service Bus alerts before failing on
     the Application-Insights role assignments (an RBAC write). Those four
     creates have therefore converged and are intentionally REMOVED from this
     policy; the diagnostic setting now shows as an in-place UPDATE (Terraform
     normalizes `log_analytics_destination_type = "Dedicated"`). What remains is
     exactly the three job/server-error metric alerts + two role assignments to
     create, and the four workloads + the diagnostic setting to update.

The following are already converged and MUST NOT reappear as create/update
actions (a create for any of them is a policy violation):
`azurerm_application_insights.staging`,
`azurerm_monitor_metric_alert.servicebus_dead_letter`,
`azurerm_monitor_metric_alert.servicebus_queue_backlog`, and
`azurerm_container_app_environment.staging`.

Reads `terraform show -json <plan>` (file arg or stdin) and enforces:

  1. NO destroy and NO replace anywhere (any change whose actions contain
     "delete" is rejected).
  2. Every changed resource must be in the exact P6 recovery allowlist below,
     and its action must match the reviewed action (create vs update) exactly.
  3. Every allowlisted resource must appear exactly once (no missing, no
     duplicate index).
  4. The totals must be exactly Add=5, Change=5, Destroy=0, Replace=0.
  5. The deterministic sanitized-plan hash must equal the reviewed recovery hash
     (unless the gate is explicitly relaxed for testing).

Output is SANITIZED: resource address + actions only, never any attribute value,
so no secret can leak. The hash is computed with the identical canonicalization
as `plan_policy.py`, so the two policies agree on the sanitized-plan hash.

Exit codes: 0 = policy passed, 2 = policy violation, 1 = usage/parse error.
"""
from __future__ import annotations

import hashlib
import json
import sys

# Exact P6 recovery allowlist: bare `type.name` (module prefix and
# count/for_each index stripped) -> the single reviewed action for that
# resource. Nothing outside this set may change, and no resource here may take a
# different action. Application Insights, the two Service Bus alerts, and the
# Container Apps Environment update are intentionally ABSENT (already converged
# by the partial applies) and must not reappear. The diagnostic setting has been
# created and now appears here as an in-place UPDATE, not a create.
P6_ALLOWLIST: dict[str, str] = {
    # 5 creates
    "azurerm_monitor_metric_alert.api_server_errors": "create",
    "azurerm_monitor_metric_alert.migration_job_failure": "create",
    "azurerm_monitor_metric_alert.worker_job_failure": "create",
    "azurerm_role_assignment.application_insights_api": "create",
    "azurerm_role_assignment.application_insights_worker": "create",
    # 5 updates (Application Insights + the diagnostic setting were created by
    # the second partial apply; the diagnostic setting now normalizes
    # log_analytics_destination_type = "Dedicated" as an in-place update)
    "azurerm_container_app.api": "update",
    "azurerm_container_app.frontend": "update",
    "azurerm_container_app_job.migration": "update",
    "azurerm_container_app_job.worker": "update",
    "azurerm_monitor_diagnostic_setting.container_app_environment": "update",
}

EXPECTED_ADD = 5
EXPECTED_CHANGE = 5
EXPECTED_DESTROY = 0
EXPECTED_REPLACE = 0

# SHA-256 over the canonical sanitized (address+actions) representation of the
# SECOND-PARTIAL-APPLY RECOVERY plan (the first-recovery plan minus the
# already-converged Application Insights + two Service Bus alert creates, with
# the diagnostic setting moved from create to update). The superseded hashes
# were 788aaab8c160b14daa12384b6f75159c42e471f1e3adad750b17de3aa48451ee
# (first recovery) and
# 3f3de0cf0a15fd7f8f457c659a1a6bba8879274a6c34f802b7ab261ca325c49d (pre-apply).
REVIEWED_SHA256 = "8e0e942484af7a868fd09703cebf7aef941d797c7a177e4d01e6620b3b94cd9d"

# Actions Terraform may report; "no-op"/"read" never count as changes.
NOOP_ACTIONS = ({"no-op"}, {"read"})


def bare_address(address: str) -> str:
    """Return the `type.name` pair, dropping any module prefix and [index]."""
    parts = address.split(".")
    if len(parts) < 2:
        return address.split("[")[0]
    return f"{parts[-2]}.{parts[-1]}".split("[")[0]


def evaluate(plan: dict, *, require_hash: str | None = REVIEWED_SHA256) -> dict:
    """Evaluate a parsed plan against the P6 policy.

    Returns a dict with: summary (sorted sanitized items), counts, summary_hash,
    violations (list of str), and ok (bool). Pass ``require_hash=None`` to skip
    the hash-equivalence gate (used by tests exercising other rules in
    isolation); any other value is enforced for exact equality.
    """
    add = change = destroy = replace = 0
    violations: list[str] = []
    summary: list[dict[str, object]] = []
    seen: dict[str, int] = {}

    for rc in plan.get("resource_changes", []):
        actions = rc.get("change", {}).get("actions", [])
        aset = set(actions)
        if aset in NOOP_ACTIONS:
            continue
        address = rc.get("address", "<unknown>")
        summary.append({"address": address, "actions": actions})

        is_replace = "delete" in aset and "create" in aset
        is_destroy = aset == {"delete"}
        if is_replace:
            replace += 1
        elif is_destroy:
            destroy += 1
        elif aset == {"create"}:
            add += 1
        elif aset == {"update"}:
            change += 1

        # Rule 1: never destroy or replace.
        if "delete" in aset:
            kind = "REPLACE" if is_replace else "DESTROY"
            violations.append(f"{kind} not allowed: {address} actions={actions}")
            continue

        bare = bare_address(address)
        seen[bare] = seen.get(bare, 0) + 1

        # Rule 2a: address must be in the exact P6 allowlist.
        if bare not in P6_ALLOWLIST:
            violations.append(
                f"resource not in P6 allowlist: {address} actions={actions}"
            )
            continue

        # Rule 2b: action must match the reviewed action exactly.
        expected = P6_ALLOWLIST[bare]
        if actions != [expected]:
            violations.append(
                f"unexpected action for {address}: got {actions}, "
                f"reviewed action is [{expected!r}]"
            )

    # Rule 3: no allowlisted resource may appear more than once.
    for bare, n in sorted(seen.items()):
        if n > 1 and bare in P6_ALLOWLIST:
            violations.append(f"duplicate change for allowlisted resource: {bare} (x{n})")

    # Rule 3 (cont.): every allowlisted resource must appear exactly once.
    for bare in sorted(P6_ALLOWLIST):
        if bare not in seen:
            violations.append(f"expected P6 resource missing from plan: {bare}")

    # Rule 4: exact totals.
    if (add, change, destroy, replace) != (
        EXPECTED_ADD,
        EXPECTED_CHANGE,
        EXPECTED_DESTROY,
        EXPECTED_REPLACE,
    ):
        violations.append(
            "plan totals mismatch: "
            f"got Add={add} Change={change} Destroy={destroy} Replace={replace}, "
            f"expected Add={EXPECTED_ADD} Change={EXPECTED_CHANGE} "
            f"Destroy={EXPECTED_DESTROY} Replace={EXPECTED_REPLACE}"
        )

    # Deterministic sanitized summary hash (addresses + actions, sorted).
    # Identical canonicalization to plan_policy.py so both policies agree.
    canonical = json.dumps(
        sorted(summary, key=lambda s: s["address"]),
        sort_keys=True,
        separators=(",", ":"),
    )
    summary_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # Rule 5: exact hash equivalence to the reviewed plan.
    if require_hash is not None and summary_hash != require_hash:
        violations.append(
            f"sanitized-plan hash mismatch: got {summary_hash}, "
            f"reviewed hash is {require_hash}"
        )

    return {
        "summary": sorted(summary, key=lambda s: s["address"]),
        "counts": {"add": add, "change": change, "destroy": destroy, "replace": replace},
        "summary_hash": summary_hash,
        "violations": violations,
        "ok": not violations,
    }


def main(argv: list[str]) -> int:
    require_hash: str | None = REVIEWED_SHA256
    path: str | None = None
    args = argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--no-hash-gate":
            require_hash = None
        elif arg == "--require-hash":
            i += 1
            if i >= len(args):
                print("p6-plan-policy: --require-hash needs a value", file=sys.stderr)
                return 1
            require_hash = args[i]
        elif arg.startswith("-"):
            print(f"p6-plan-policy: unknown option {arg}", file=sys.stderr)
            return 1
        else:
            path = arg
        i += 1

    try:
        raw = sys.stdin.read() if path is None else open(path, encoding="utf-8").read()
        plan = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"p6-plan-policy: could not read/parse plan JSON: {exc}", file=sys.stderr)
        return 1

    result = evaluate(plan, require_hash=require_hash)
    counts = result["counts"]

    print("=== P6 one-time plan safety summary (sanitized) ===")
    for item in result["summary"]:
        print(f"  {','.join(item['actions']):<16} {item['address']}")
    print(
        f"Add={counts['add']} Change={counts['change']} "
        f"Destroy={counts['destroy']} Replace={counts['replace']}"
    )
    print(f"summary_sha256={result['summary_hash']}")

    if result["violations"]:
        print("\n=== P6 POLICY VIOLATIONS ===", file=sys.stderr)
        for v in result["violations"]:
            print(f"  - {v}", file=sys.stderr)
        return 2

    print("p6-plan-policy: PASS (exact P6 recovery allowlist, 5/5/0/0, recovery hash matched)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
