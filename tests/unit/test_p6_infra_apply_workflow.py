"""Deterministic guards for the TEMPORARY P6 one-time apply workflow.

These tests pin the CONTROL-PLANE / RELEASE-PAYLOAD checkout separation that
recovers from the failure of run 33959502997. That run used a single checkout
at ``release_sha`` and then executed ``p6_plan_policy.py`` FROM THAT RELEASE
CHECKOUT, where a superseded policy still lives — so the release payload
shadowed the control-plane policy and rejected the correct live recovery plan.

The fix keeps two pinned checkouts:

  * ``control-plane/`` at the workflow host commit (``github.sha`` on main) —
    the source of the CURRENT recovery ``p6_plan_policy.py``;
  * ``release/`` at exactly ``release_sha`` — the source of the Terraform root
    and the application/deploy scripts.

Terraform must run from ``release/`` and the policy must run from
``control-plane/`` in BOTH the plan job and the apply job's re-plan.

Contract
--------
The CURRENT authoritative contract is the SECOND partial-apply recovery:
5 add / 5 change / 0 destroy / 0 replace, hash 8e0e9424...  The two superseded
contracts (first recovery 9/4 hash 788aaab8...; original pre-apply 9/5 hash
3f3de0cf...) may appear only as documented history, never as the active
require-hash target.

These are text-level assertions on the workflow YAML so they run with no
third-party dependency (the CI unit image has no PyYAML). Remove alongside
``.github/workflows/p6-infra-apply.yml`` after P6 staging validation.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "p6-infra-apply.yml"

RELEASE_SHA = "dd5151b27b7121e555b1f26eb455c2dd36f28495"  # documented, not enforced as input
# CURRENT authoritative contract.
RECOVERY_HASH = "8e0e942484af7a868fd09703cebf7aef941d797c7a177e4d01e6620b3b94cd9d"
# Historical only — never the active require-hash target.
FIRST_RECOVERY_HASH = "788aaab8c160b14daa12384b6f75159c42e471f1e3adad750b17de3aa48451ee"
ORIGINAL_PRE_APPLY_HASH = "3f3de0cf0a15fd7f8f457c659a1a6bba8879274a6c34f802b7ab261ca325c49d"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _job_slice(text: str, job_key: str) -> str:
    """Return the body of a top-level job (``plan``/``apply``) under ``jobs:``.

    Slices from the 2-space-indented job header to the next 2-space-indented
    job header (or EOF), so per-job assertions cannot leak across jobs.
    """
    start = text.index(f"\n  {job_key}:\n")
    rest = text[start + 1 :]
    others = [i for key in ("plan", "apply") if key != job_key
              for i in [rest.find(f"\n  {key}:\n")] if i != -1]
    end = min(others) if others else len(rest)
    return rest[:end]


def test_workflow_file_exists() -> None:
    assert WORKFLOW.is_file(), f"workflow missing at {WORKFLOW}"


def test_env_points_terraform_at_release_and_policy_at_control_plane() -> None:
    text = _text()
    # (3) Terraform root resolves under the RELEASE checkout.
    assert "TF_ROOT: release/infra/terraform/environments/staging" in text
    # (4) Policy executable resolves under the CONTROL-PLANE checkout.
    assert "P6_POLICY: control-plane/infra/terraform/scripts/p6_plan_policy.py" in text
    # (7) Smoke script resolves under the RELEASE checkout.
    assert "SMOKE_SCRIPT: release/scripts/ci/smoke-staging.sh" in text
    # The current recovery contract hash is pinned; the superseded hashes appear
    # only as documented history, never as the active require-hash target.
    assert f"P6_REVIEWED_HASH: {RECOVERY_HASH}" in text


def test_both_jobs_have_two_pinned_checkouts_into_separate_paths() -> None:
    # (1) release checkout is explicitly separated from control checkout, and
    # (2) release SHA provenance is preserved, in BOTH jobs.
    for job_key in ("plan", "apply"):
        body = _job_slice(_text(), job_key)
        assert "ref: ${{ github.sha }}" in body, f"{job_key}: control checkout not pinned to host commit"
        assert "path: control-plane" in body, f"{job_key}: missing control-plane checkout path"
        assert "ref: ${{ inputs.release_sha }}" in body, f"{job_key}: release checkout not pinned to release_sha"
        assert "path: release" in body, f"{job_key}: missing release checkout path"
        # release HEAD is re-verified against the requested release_sha.
        assert 'git -C "$GITHUB_WORKSPACE/release" rev-parse HEAD' in body, \
            f"{job_key}: release HEAD not re-verified"
        assert 'git -C "$GITHUB_WORKSPACE/control-plane" rev-parse HEAD' in body, \
            f"{job_key}: control-plane HEAD not re-verified"


def test_release_sha_input_is_strict_40_hex_validated() -> None:
    # (2) strict 40-hex validation of the release_sha input remains.
    assert "^[0-9a-f]{40}$" in _text()


def test_policy_runs_only_from_control_plane_never_from_release() -> None:
    # (5)+(6) both jobs invoke the policy via the control-plane $P6_POLICY var,
    # and never resolve it from a release/bare path (the shadowing bug).
    text = _text()
    assert text.count('python3 "$P6_POLICY"') == 2, "expected the control-plane policy invocation in both jobs"
    # The pre-fix, shadow-prone invocations must be gone.
    assert "python3 infra/terraform/scripts/p6_plan_policy.py" not in text
    assert "python3 release/infra/terraform/scripts/p6_plan_policy.py" not in text
    for job_key in ("plan", "apply"):
        body = _job_slice(text, job_key)
        assert 'python3 "$P6_POLICY"' in body, f"{job_key}: policy not run from control plane"


def test_terraform_never_runs_from_control_plane() -> None:
    # Terraform must operate only on the release payload via $TF_ROOT.
    text = _text()
    assert "control-plane/infra/terraform/environments" not in text
    # every terraform -chdir uses the $TF_ROOT variable (release-rooted).
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("terraform -chdir="):
            assert '-chdir="$TF_ROOT"' in stripped, f"terraform not chdir'd to $TF_ROOT: {stripped}"


def test_control_policy_provenance_guard_present_in_both_jobs() -> None:
    # Runtime guard proving the control policy is the 5/5 second-recovery
    # contract and that the already-converged resources are absent from its
    # allowlist — in BOTH jobs.
    text = _text()
    assert text.count("EXPECTED_ADD = 5") == 2
    assert text.count("EXPECTED_CHANGE = 5") == 2
    # The Container Apps Environment update must not be allowlisted (converged in
    # the first partial apply).
    assert text.count('"azurerm_container_app_environment\\.staging"[[:space:]]*:') == 2
    # Application Insights + the two Service Bus alerts converged in the second
    # partial apply; their creates must not be allowlisted either.
    assert text.count('"azurerm_application_insights\\.staging"[[:space:]]*:') == 2
    assert text.count('"azurerm_monitor_metric_alert\\.servicebus_dead_letter"[[:space:]]*:') == 2
    assert text.count('"azurerm_monitor_metric_alert\\.servicebus_queue_backlog"[[:space:]]*:') == 2
    for job_key in ("plan", "apply"):
        body = _job_slice(text, job_key)
        assert "EXPECTED_ADD = 5" in body, f"{job_key}: missing control-policy Add=5 guard"
        assert "EXPECTED_CHANGE = 5" in body, f"{job_key}: missing control-policy Change=5 guard"
        # The guard checks the recovery hash via the pinned env var (the literal
        # hash lives once in the env block, asserted separately).
        assert 'grep -q "$P6_REVIEWED_HASH"' in body, \
            f"{job_key}: control-policy recovery-hash guard not wired to $P6_REVIEWED_HASH"


def test_smoke_runs_from_release_checkout() -> None:
    # (7) release runtime helper is executed from the release checkout.
    body = _job_slice(_text(), "apply")
    assert 'bash "$SMOKE_SCRIPT"' in body


def test_dispatch_only_trigger() -> None:
    # (8) workflow remains workflow_dispatch-only.
    text = _text()
    assert "workflow_dispatch:" in text
    for forbidden in ("\n  push:", "\n  pull_request:", "\n  schedule:", "\n  workflow_run:"):
        assert forbidden not in text, f"unexpected trigger present: {forbidden!r}"


def test_run_apply_gating_and_mandatory_staging_environment() -> None:
    apply = _job_slice(_text(), "apply")
    # (9) apply is gated on the run_apply input.
    assert "if: ${{ inputs.run_apply }}" in apply
    # (10) apply requires the human-reviewed `staging` environment.
    assert "environment: staging" in apply
    # plan uses the reader/plan environment, never `staging`.
    plan = _job_slice(_text(), "plan")
    assert "environment: staging-plan" in plan


def test_superseded_hashes_are_never_the_active_contract() -> None:
    # The old 9/4 and 9/5 hashes may appear only in explanatory comments, never
    # as the P6_REVIEWED_HASH value or a --require-hash argument.
    text = _text()
    for old in (FIRST_RECOVERY_HASH, ORIGINAL_PRE_APPLY_HASH):
        assert f"P6_REVIEWED_HASH: {old}" not in text
        assert f"--require-hash {old}" not in text
