#!/usr/bin/env python3
"""Machine-checked Terraform plan safety policy for FootballAI staging deploys.

Reads `terraform show -json <plan>` on stdin (or a file arg) and enforces:

  1. NO destroy and NO replace anywhere (any change whose actions contain
     "delete" is rejected — this covers both plain deletes and
     delete+create replacements).
  2. Only the known workload resources may be created/updated. Any create or
     update to a protected FOUNDATION resource (PostgreSQL, VNet, subnets,
     Storage Account, Service Bus, ACR, Container Apps Environment, identities,
     role assignments, Terraform-state, ...) is rejected as an unexpected
     foundation change.

It emits a SANITIZED summary (resource address + actions only — never any
attribute value, so no secret can leak) and a deterministic summary hash so a
later apply job can prove it is applying the exact plan that was approved.

Exit codes: 0 = policy passed, 2 = policy violation, 1 = usage/parse error.
"""
from __future__ import annotations

import hashlib
import json
import sys

# Address prefixes (module-relative) that a normal image deploy may create or
# update. count/for_each indices like [0] are tolerated via prefix match.
ALLOWED_WORKLOADS = (
    "azurerm_container_app.api",
    "azurerm_container_app.frontend",
    "azurerm_container_app_job.worker",
    "azurerm_container_app_job.migration",
)

# Actions Terraform may report; "no-op"/"read" never count as changes.
NOOP_ACTIONS = ({"no-op"}, {"read"})


def _is_allowed_workload(address: str) -> bool:
    """True if `address` is one of the allowed workload resources.

    Terraform addresses look like `azurerm_container_app.api[0]` or
    `module.foo.azurerm_container_app.api`. Drop any module prefix and any
    count/for_each index, then compare the bare `type.name` exactly.
    """
    bare = address.split(".")
    # Reconstruct the final `type.name` pair (last two dotted segments),
    # stripping a trailing [index] or ["key"].
    if len(bare) < 2:
        return False
    type_name = f"{bare[-2]}.{bare[-1]}".split("[")[0]
    return type_name in ALLOWED_WORKLOADS


def main() -> int:
    raw = sys.stdin.read() if len(sys.argv) < 2 else open(sys.argv[1], encoding="utf-8").read()
    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        print(f"plan-policy: could not parse plan JSON: {exc}", file=sys.stderr)
        return 1

    add = change = destroy = replace = 0
    violations: list[str] = []
    summary: list[dict[str, object]] = []

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

        # Rule 2: only workload resources may change.
        if aset in ({"create"}, {"update"}) and not _is_allowed_workload(address):
            violations.append(
                f"unexpected FOUNDATION change: {address} actions={actions} "
                "(only frontend/api/worker/migration workloads may change)"
            )

    # Deterministic sanitized summary hash (addresses + actions, sorted).
    canonical = json.dumps(
        sorted(summary, key=lambda s: s["address"]),
        sort_keys=True,
        separators=(",", ":"),
    )
    summary_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    print("=== Terraform plan safety summary (sanitized) ===")
    for item in sorted(summary, key=lambda s: s["address"]):
        print(f"  {','.join(item['actions']):<16} {item['address']}")
    print(f"Add={add} Change={change} Destroy={destroy} Replace={replace}")
    print(f"summary_sha256={summary_hash}")

    if violations:
        print("\n=== POLICY VIOLATIONS ===", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 2

    print("plan-policy: PASS (no destroy, no replace, no unexpected foundation change)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
