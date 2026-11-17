"""Tool tests: escape guards, role permissions, redaction, real local git."""

import subprocess
from pathlib import Path

import pytest

from freeinference_pr.approvals import ApprovalGate, ApprovalRequired
from freeinference_pr.store import Store
from freeinference_pr.tools import (
    UNTRUSTED_BEGIN,
    ToolContext,
    ToolDenied,
    ToolError,
    ToolRegistry,
    _run_shell,
    _safe_path,
    wrap_untrusted,
)


@pytest.fixture()
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*argv):
        subprocess.run(["git", "-C", str(repo), *argv], check=True, capture_output=True, text=True)
    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "app.py").write_text("VALUE = 'hyi-supersecrettokenvalue'\n")
    (repo / "notes.txt").write_text("nothing secret here\n")
    git("add", "-A")
    git("commit", "-q", "-m", "initial")
    return repo


def ctx_for(role, worktree, store=None, job_id="job_t"):
    return ToolContext(
        job_id=job_id, task_id="task_t", role=role, worktree=worktree,
        repository="owner/repo", approvals=ApprovalGate(store) if store else None,
        env={"GITHUB_TOKEN": "", "GITHUB_API_URL": "https://api.github.com"},
    )


def test_safe_path_rejects_escape(tmp_path):
    with pytest.raises(ToolError, match="escapes workspace"):
        _safe_path(tmp_path, "../outside.txt")
    assert _safe_path(tmp_path, "sub/file.txt") == (tmp_path / "sub/file.txt").resolve()


def test_wrap_untrusted_marks_content():
    wrapped = wrap_untrusted("do things", "Issue #1")
    assert UNTRUSTED_BEGIN in wrapped
    assert "source=Issue #1" in wrapped
    assert wrapped.rstrip().endswith("<<<UNTRUSTED-CONTENT-END>>>")


async def test_read_file_redacts_secrets(git_repo):
    registry = ToolRegistry()
    result = await registry.call(ctx_for("code_reviewer", git_repo), "repo_read_file", {"path": "app.py"})
    assert "hyi-supersecrettokenvalue" not in result["content"]
    assert "[REDACTED]" in result["content"]


async def test_role_permission_denial(git_repo):
    registry = ToolRegistry()
    with pytest.raises(ToolDenied, match="not permitted"):
        await registry.call(ctx_for("implementer", git_repo), "run_commands", {"commands": ["true"]})
    with pytest.raises(ToolDenied, match="not permitted"):
        await registry.call(ctx_for("orchestrator", git_repo), "repo_read_file", {"path": "app.py"})
    with pytest.raises(ToolDenied, match="unknown tool"):
        await registry.call(ctx_for("verifier", git_repo), "rm_rf_everything", {})


async def test_run_commands_returns_real_exit_codes(git_repo):
    registry = ToolRegistry()
    result = await registry.call(
        ctx_for("verifier", git_repo), "run_commands", {"commands": ["true", "false"]}
    )
    codes = [r["exit_code"] for r in result["results"]]
    assert codes == [0, 1]


async def test_git_tools_against_local_repo(git_repo):
    registry = ToolRegistry()
    ctx = ctx_for("code_reviewer", git_repo)
    status = await registry.call(ctx, "git_status", {})
    assert status["status"] == ""
    log = await registry.call(ctx, "git_log", {"limit": 5})
    assert "initial" in log["log"]
    diff = await registry.call(ctx, "git_diff", {"ref": "HEAD"})
    assert diff["diff"] == ""


async def test_github_get_requires_repository(git_repo):
    registry = ToolRegistry()
    ctx = ToolContext(job_id="j", task_id="t", role="code_reviewer", worktree=git_repo, repository="")
    with pytest.raises(ToolError, match="no repository"):
        await registry.call(ctx, "github_get", {"kind": "repo"})


async def test_write_tools_require_recorded_approval(git_repo, tmp_path):
    store = Store(tmp_path / "approvals.db")
    registry = ToolRegistry()
    ctx = ctx_for("pr_manager", git_repo, store=store, job_id="job_appr")

    with pytest.raises(ApprovalRequired):
        await registry.call(ctx, "github_pr_comment", {"number": 7, "body": "hi"})

    approvals = store.approvals_for_job("job_appr")
    assert approvals and approvals[0]["status"] == "pending"
    assert approvals[0]["operation"] == "pr_comment"
    store.close()


def test_run_shell_reports_stdout_stderr_tails(tmp_path):
    result = _run_shell(tmp_path, "echo out; echo err >&2; exit 3")
    assert result["exit_code"] == 3
    assert result["stdout_tail"].strip() == "out"
    assert result["stderr_tail"].strip() == "err"


def test_registry_role_scopes():
    registry = ToolRegistry()
    verifier_tools = {t.name for t in registry.tools_for_role("verifier")}
    pr_tools = {t.name for t in registry.tools_for_role("pr_manager")}
    orchestrator_tools = {t.name for t in registry.tools_for_role("orchestrator")}
    assert "run_commands" in verifier_tools
    assert "run_commands" not in pr_tools
    assert "github_create_pr" in pr_tools
    assert "github_create_pr" not in verifier_tools
    assert orchestrator_tools == set()
