"""Approval gate: privileged operations execute only with a recorded approval.

The gate is the single enforcement point used by write-capable tools. Agents may
propose privileged operations, but the tool handler refuses to act until the
store holds an 'approved' record for the exact operation and details.

One user authorization covers the mechanical sub-steps it implies: approving
'publish_branch_and_pr' (push branch X at commit Y, open the PR) satisfies the
derived 'create_branch', 'commit_files', and 'create_pull_request' checks for
that same repository/branch. It grants nothing else.
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


DERIVED_OPERATIONS = {
    "create_branch": "publish_branch_and_pr",
    "commit_files": "publish_branch_and_pr",
    "create_pull_request": "publish_branch_and_pr",
}


class ApprovalGate:
    def __init__(self, store: Store) -> None:
        self.store = store

    def require_approved(self, job_id: str, operation: str, details: dict[str, Any]) -> None:
        operations = [operation]
        derived = DERIVED_OPERATIONS.get(operation)
        if derived:
            operations.append(derived)
        for candidate in operations:
            for approval in self.store.approvals_for_job(job_id):
                if approval["operation"] != candidate:
                    continue
                if approval["status"] == "approved" and _details_match(approval["details"], details):
                    return
        pending = self.store.request_approval(job_id, operation, details)
        raise ApprovalRequired(pending)


def _details_match(recorded: dict[str, Any], requested: dict[str, Any]) -> bool:
    """The approved record must cover the requested operation's identifying fields."""
    for key, value in requested.items():
        if key in recorded and recorded[key] != value:
            return False
    return True
