"""Regression tests for Mission-3 audit corrections.

Covers: strict approval identity matching, credential-free sandbox env for
untrusted subprocesses, path containment, and non-destructive worktree
recovery.
"""

import json
import os
import subprocess

import pytest

from freeinference_pr.approvals import ApprovalGate, ApprovalRequired
from freeinference_pr.engine import prepare_worktree
from freeinference_pr.store import Store
from freeinference_pr.tools import ToolContext, ToolRegistry, ToolError, _sandbox_env


@pytest.fixture()
def store(tmp_path):
    store = Store(str(tmp_path / "state.db"))
    job = store.create_job(
        "issue", "octo/example", "test objective", issue_number=1
    )
    return store, job["id"]


def approve(store, job_id, operation, details):
    gate = ApprovalGate(store)
    record = store.request_approval(job_id, operation, details)
    store.decide_approval(record["id"], "approved")
    return record["id"]


def test_partial_approval_record_is_rejected(store):
    store, job_id = store
    approve(store, job_id, "publish_branch_and_pr",
            {"repository": "octo/example", "branch": "fi-pr/x"})
    gate = ApprovalGate(store)
    with pytest.raises(ApprovalRequired):
        gate.require_approved(
            job_id,
            "publish_branch_and_pr",
            {"repository": "octo/example", "branch": "fi-pr/x", "commit": "0" * 40},
        )


def test_wrong_branch_is_rejected(store):
    store, job_id = store
    approve(store, job_id, "publish_branch_and_pr",
            {"repository": "octo/example", "branch": "b1"})
    gate = ApprovalGate(store)
    with pytest.raises(ApprovalRequired):
        gate.require_approved(
            job_id,
            "publish_branch_and_pr",
            {"repository": "octo/example", "branch": "b2", "commit": "0" * 40},
        )


def test_wrong_repository_is_rejected(store):
    store, job_id = store
    approve(store, job_id, "publish_branch_and_pr",
            {"repository": "octo/one", "branch": "b1"})
    gate = ApprovalGate(store)
    with pytest.raises(ApprovalRequired):
        gate.require_approved(
            job_id,
            "publish_branch_and_pr",
            {"repository": "octo/two", "branch": "b1", "commit": "0" * 40},
        )


def test_full_matching_approval_is_accepted(store):
    store, job_id = store
    approve(store, job_id, "publish_branch_and_pr",
            {"repository": "octo/example", "branch": "b1", "commit": "0" * 40})
    gate = ApprovalGate(store)
    gate.require_approved(
        job_id,
        "publish_branch_and_pr",
        {"repository": "octo/example", "branch": "b1", "commit": "0" * 40},
    )


def test_derived_create_branch_requires_branch_binding(store):
    store, job_id = store
    approve(store, job_id, "publish_branch_and_pr",
            {"repository": "octo/example", "branch": "b1"})
    gate = ApprovalGate(store)
    gate.require_approved(
        job_id, "create_branch", {"repository": "octo/example", "branch": "b1"}
    )
    with pytest.raises(ApprovalRequired):
        gate.require_approved(
            job_id, "create_branch", {"repository": "octo/example", "branch": "other"}
        )


def test_derived_create_pr_maps_head_to_branch(store):
    store, job_id = store
    approve(store, job_id, "publish_branch_and_pr",
            {"repository": "octo/example", "branch": "b1"})
    gate = ApprovalGate(store)
    gate.require_approved(
        job_id, "create_pull_request",
        {"repository": "octo/example", "head": "b1", "base": "main"},
    )
    with pytest.raises(ApprovalRequired):
        gate.require_approved(
            job_id, "create_pull_request",
            {"repository": "octo/example", "head": "other", "base": "main"},
        )


def test_pr_comment_approval_covers_only_its_own_pr(store):
    store, job_id = store
    approve(store, job_id, "pr_comment",
            {"repository": "octo/example", "pr_number": 5})
    gate = ApprovalGate(store)
    gate.require_approved(
        job_id, "pr_comment", {"repository": "octo/example", "pr_number": 5}
    )
    with pytest.raises(ApprovalRequired):
        gate.require_approved(
            job_id, "pr_comment", {"repository": "octo/example", "pr_number": 6}
        )


def test_publish_approval_does_not_authorize_pr_comment(store):
    store, job_id = store
    approve(store, job_id, "publish_branch_and_pr",
            {"repository": "octo/example", "branch": "b1"})
    gate = ApprovalGate(store)
    with pytest.raises(ApprovalRequired):
        gate.require_approved(
            job_id, "pr_comment", {"repository": "octo/example", "pr_number": 5}
        )


def test_sandbox_env_excludes_all_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("FREEINFERENCE_API_KEY", "fi-secret")
    monkeypatch.setenv("FREEINFERENCE_BASE_URL", "https://freeinference.org/v1")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.com")
    env = _sandbox_env(tmp_path)
    joined = json.dumps(env)
    for secret in ("fi-secret", "gh-secret"):
        assert secret not in joined
    for key in ("FREEINFERENCE_API_KEY", "GITHUB_TOKEN", "GITHUB_API_URL"):
        assert key not in env
    assert "PATH" in env
    assert "HOME" in env


def test_run_tests_cannot_see_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("FREEINFERENCE_API_KEY", "fi-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")
    registry = ToolRegistry()
    ctx = ToolContext(
        job_id="j", task_id="t", role="verifier", worktree=tmp_path
    )
    import asyncio

    result = asyncio.run(
        registry.call(ctx, "run_commands", {"commands": ["env"]})
    )
    text = json.dumps(result)
    assert "fi-secret" not in text
    assert "gh-secret" not in text
    assert "FREEINFERENCE_API_KEY" not in text


def test_safe_path_rejects_parent_traversal(tmp_path):
    registry = ToolRegistry()
    ctx = ToolContext(job_id="j", task_id="t", role="implementer", worktree=tmp_path)
    handler = registry._tools["repo_read_file"].handler
    import asyncio

    for bad in ("../outside.txt", "sub/../../outside.txt"):
        with pytest.raises(ToolError):
            asyncio.run(handler(ctx, bad))
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "file.txt").write_text("ok")
    result = asyncio.run(handler(ctx, "sub/file.txt"))
    assert result["content"] == "ok"
    with pytest.raises(ToolError):
        asyncio.run(handler(ctx, "sub/../nonexistent.txt"))


def test_safe_path_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / "outside-dir-regress"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("secret")
    link = tmp_path / "link"
    link.symlink_to(outside)
    registry = ToolRegistry()
    ctx = ToolContext(job_id="j", task_id="t", role="implementer", worktree=tmp_path)
    handler = registry._tools["repo_read_file"].handler
    import asyncio

    with pytest.raises(ToolError):
        asyncio.run(handler(ctx, "link/secret.txt"))


def test_prepare_worktree_reuses_existing_checkout(tmp_path):
    repo_dir = tmp_path / "job_rec1" / "repo"
    repo_dir.mkdir(parents=True)
    env = dict(os.environ)
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "t@example.com"
    env["GIT_CONFIG_GLOBAL"] = str(tmp_path / "gitconfig")
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"

    def run(*args):
        subprocess.run(args, cwd=repo_dir, check=True, env=env, capture_output=True)

    run("git", "init", "-q")
    run("git", "checkout", "-q", "-b", "main")
    (repo_dir / "f.txt").write_text("precious\n")
    run("git", "add", "f.txt")
    run("git", "commit", "-q", "-m", "init")
    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_dir, check=True,
        capture_output=True, text=True, env=env,
    ).stdout.strip()

    worktree = prepare_worktree(tmp_path, "job_rec1", "octo/example", "", "fi-pr/x")
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree.path, check=True,
        capture_output=True, text=True, env=env,
    ).stdout.strip()

    assert head_after == head_before
    assert (worktree.path / "f.txt").read_text() == "precious\n"

    prepare_worktree(tmp_path, "job_rec1", "octo/example", "", "fi-pr/x")
    head_final = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree.path, check=True,
        capture_output=True, text=True, env=env,
    ).stdout.strip()
    assert head_final == head_before
