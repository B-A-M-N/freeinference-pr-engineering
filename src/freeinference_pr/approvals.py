"""Approval gate: privileged operations execute only with a recorded approval.

The gate is the single enforcement point used by write-capable tools. Agents may
propose privileged operations, but the tool handler refuses to act until the
store holds an 'approved' record for the exact operation and details.

Matching is strict: every identifying field the operation requires must be
present in the recorded approval and equal to the requested value. A record
that omits a required identity binding never satisfies a request — an approval
for "push branch X of repo R" cannot be stretched to cover repo S, branch T, or
an operation whose identity was never approved.

One user authorization covers the mechanical sub-steps it implies: approving
'publish_branch_and_pr' (push branch X at commit Y, open the PR) satisfies the
derived 'create_branch', 'commit_files', and 'create_pull_request' checks for
that same repository/branch via an explicit field mapping. It grants nothing
else.
"""

from __future__ import annotations

import json
from typing import Any

from freeinference_pr.store import Store


class ApprovalRequired(Exception):
    def __init__(self, approval: dict[str, Any]) -> None:
        self.approval = approval
        details = json.dumps(approval.get("details", {}), default=str)
        super().__init__(
            f"operation '{approval['operation']}' requires approval "
            f"(approval_id={approval['id']}, details={details})"
        )


# operation -> (parent authorization, requested-field -> recorded-field map,
#               identity fields the parent record can verify)
# The PR base branch is not bound by the parent approval: it is a branch of the
# already-approved repository, resolved by the engine from repository evidence.
DERIVED_OPERATIONS: dict[str, tuple[str, dict[str, str], tuple[str, ...]]] = {
    "create_branch": ("publish_branch_and_pr", {}, ("repository", "branch")),
    "commit_files": ("publish_branch_and_pr", {}, ("repository", "branch")),
    "create_pull_request": ("publish_branch_and_pr", {"head": "branch"}, ("repository", "head")),
}

# Identifying fields each operation must have approved; a recorded approval
# missing any of these bindings is rejected, never silently accepted.
REQUIRED_IDENTITY: dict[str, tuple[str, ...]] = {
    "publish_branch_and_pr": ("repository", "branch", "commit"),
    "create_branch": ("repository", "branch"),
    "commit_files": ("repository", "branch"),
    "create_pull_request": ("repository", "head", "base"),
    "pr_comment": ("repository", "pr_number"),
}


class ApprovalGate:
    def __init__(self, store: Store) -> None:
        self.store = store

    def require_approved(self, job_id: str, operation: str, details: dict[str, Any]) -> None:
        checks: list[tuple[str, dict[str, str], tuple[str, ...]]] = [
            (operation, {}, REQUIRED_IDENTITY.get(operation, tuple(details.keys())))
        ]
        derived = DERIVED_OPERATIONS.get(operation)
        if derived:
            checks.append((derived[0], derived[1], derived[2]))
        for candidate, field_map, required in checks:
            for approval in self.store.approvals_for_job(job_id):
                if approval["operation"] != candidate:
                    continue
                if approval["status"] == "approved" and _details_match(
                    approval["details"], details, field_map, required
                ):
                    return
        pending = self.store.request_approval(job_id, operation, details)
        raise ApprovalRequired(pending)


def _details_match(
    recorded: dict[str, Any],
    requested: dict[str, Any],
    field_map: dict[str, str] | None = None,
    required: tuple[str, ...] | None = None,
) -> bool:
    """Every required identity binding must exist in the recorded approval and match.

    Returns False when a required field is absent from the record — a partial
    approval must not authorize an operation it cannot fully identify.
    """
    field_map = field_map or {}
    required = required or tuple(requested.keys())
    for key in required:
        recorded_key = field_map.get(key, key)
        if recorded_key not in recorded:
            return False
        if recorded[recorded_key] != requested.get(key):
            return False
    return True
