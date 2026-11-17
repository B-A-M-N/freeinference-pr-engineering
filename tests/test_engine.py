"""Engine end-to-end tests: local git fixture, scripted FI, fake GitHub REST.

No network: worktree cloning is redirected to a local fixture repository and
the GitHub REST layer is replaced by an in-memory fake.
"""

import json
import subprocess

import pytest

from freeinference_pr.config import FIConfig
from freeinference_pr.engine import Engine, Worktree
from freeinference_pr.fi_provider import ChatResult, Usage
from freeinference_pr.runtime import AgentRuntime
from freeinference_pr.store import Store
from freeinference_pr.tools import ToolRegistry

REPO = "octo/example"


@pytest.fixture()
def origin(tmp_path):
    repo = tmp_path / "origin"
    repo.mkdir()

    def git(*argv):
        subprocess.run(["git", "-C", str(repo), *argv], check=True, capture_output=True, text=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "app.py").write_text("VALUE = 1\n")
    (repo / "README.md").write_text("# example\n")
    git("add", "-A")
    git("commit", "-q", "-m", "initial")
    return repo


@pytest.fixture()
def env(tmp_path, origin):
    return {
        "FI_STATE_PATH": str(tmp_path / "state.db"),
        "FI_WORKSPACE_ROOT": str(tmp_path / "workspaces"),
        "FI_TASK_TIMEOUT": "60",
        "FI_MAX_CORRECTION_CYCLES": "1",
        "FI_MAX_AGENT_TURNS": "6",
        "FI_BACKOFF_BASE": "0.01",
        "FI_BACKOFF_MAX": "0.02",
    }


@pytest.fixture()
def patch_worktree(monkeypatch, origin):
    def _prepare(workspace_root, job_id, repository, base_ref, branch):
        repo_dir = workspace_root / job_id / "repo"
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", str(origin), str(repo_dir)], check=True,
                       capture_output=True)
        worktree = Worktree(repo_dir, repository, base_ref, branch)
        if base_ref:
            worktree.git("checkout", base_ref)
        worktree.git("switch", "-c", branch)
        return worktree

    monkeypatch.setattr("freeinference_pr.engine.prepare_worktree", _prepare)


class FakeFI:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    async def chat(self, model, messages, tools=None, temperature=0.2, **kwargs):
        self.calls.append({"model": model, "messages": messages, "tools": tools})
        return self.responder(model, messages)

    async def close(self):
        return None


def final_json(payload):
    return ChatResult(content=json.dumps(payload), usage=Usage(2, 2, 4),
                      finish_reason="stop", model="fake")


def last_user_text(messages):
    return [m["content"] or "" for m in messages if m["role"] == "user"][-1]


class FakeGitHub:
    """In-memory stand-in for tools._github_request."""

    def __init__(self):
        self.operations = []
        self.prs = {}
        self.refs = {}
        self.next_number = 1

    async def __call__(self, ctx, method, path, json_body=None):
        self.operations.append((method, path, json_body))
        if method == "GET" and path.startswith("/repos/") and path.count("/") == 3:
            return {"default_branch": "main", "full_name": REPO}
        if method == "GET" and "/git/ref/heads/" in path:
            branch = path.rsplit("/", 1)[-1]
            sha = self.refs.get(branch, "base0sha")
            return {"object": {"sha": sha}}
        if method == "POST" and path.endswith("/git/refs"):
            branch = json_body["ref"].split("/")[-1]
            self.refs[branch] = json_body["sha"]
            return {"ref": json_body["ref"]}
        if method == "GET" and "/git/commits/" in path:
            return {"tree": {"sha": "tree0"}, "sha": path.rsplit("/", 1)[-1]}
        if method == "POST" and path.endswith("/git/blobs"):
            return {"sha": f"blob{len(self.operations)}"}
        if method == "POST" and path.endswith("/git/trees"):
            return {"sha": f"tree{len(self.operations)}"}
        if method == "POST" and path.endswith("/git/commits"):
            return {"sha": f"commit{len(self.operations)}"}
        if method == "PATCH" and "/git/refs/heads/" in path:
            branch = path.rsplit("/", 1)[-1]
            self.refs[branch] = json_body["sha"]
            return {"ok": True}
        if method == "GET" and "/pulls?" in path:
            return {"result": []}
        if method == "POST" and path.endswith("/pulls"):
            number = self.next_number
            self.next_number += 1
            self.prs[number] = json_body
            return {"number": number, "html_url": f"https://github.com/{REPO}/pull/{number}"}
        if method == "POST" and "/issues/" in path and path.endswith("/comments"):
            return {"id": 1}
        raise AssertionError(f"unexpected fake github call: {method} {path}")


@pytest.fixture()
def patch_github(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr("freeinference_pr.tools._github_request", fake)
    return fake


ANALYSIS = {
    "summary": "one-file change",
    "affected_components": ["app.py"],
    "production_paths": [],
    "implementation_units": [{"title": "bump value", "guidance": "set VALUE to 2",
                              "paths": ["app.py"], "security_sensitive": False}],
    "findings": [],
    "open_questions": [],
}

EDITS = {
    "edits": [{"path": "app.py", "content": "VALUE = 2\n", "action": "write"}],
    "commit_message": "bump VALUE to 2",
    "regression_tests": ["python -c 'import app; assert app.VALUE == 2'"],
    "notes": "",
}

CLEAN_REVIEW = {"assessment": "correct", "findings": [], "missed_paths": [],
                "unsupported_assumptions": []}

BLOCKER_REVIEW = {"assessment": "wrong", "findings": [{"title": "off-by-one", "severity": "high",
                                                       "evidence": ["app.py:1"], "detail": "bad"}],
                  "missed_paths": [], "unsupported_assumptions": []}


def make_engine(env, responder):
    config = FIConfig(env)
    store = Store(config.state_path)
    client = FakeFI(responder)
    engine = Engine(config, store, client, ToolRegistry())
    return engine, store, client


def scripted_responder(review_payload=None, verify_passed=True, publish=True,
                       test_commands=("python -c 'import app; print(app.VALUE)'",)):
    review_payload = review_payload or CLEAN_REVIEW

    def responder(model, messages):
        text = last_user_text(messages)
        if text.startswith("# Task\nAnalyze"):
            return final_json(ANALYSIS)
        if "Implement the unit" in text:
            return final_json(EDITS)
        if "Choose the minimal relevant test" in text:
            return final_json({"commands": list(test_commands), "rationale": "smoke"})
        if "Independently review" in text or "Security review" in text:
            return final_json(review_payload)
        if "Verify the combined changes" in text or "Interpret the CI status" in text:
            return final_json({"interpretation": "tests passed", "passed": verify_passed,
                               "unresolved": [], "notes": []})
        if "Prepare the PR package" in text:
            return final_json({"title": "Bump VALUE", "body_markdown": "## What\nBump VALUE to 2.",
                               "publish": publish, "notes": ""})
        if "Draft the review report" in text:
            return final_json({"title": "Review", "body_markdown": "## Findings\nNone.",
                               "publish": publish, "notes": ""})
        raise AssertionError(f"unmatched agent prompt: {text[:120]}")

    return responder


def make_job(store, kind="request", **kwargs):
    return store.create_job(kind, REPO, "bump VALUE", **kwargs)


async def test_implement_job_reaches_awaiting_approval(env, origin, patch_worktree):
    engine, store, client = make_engine(env, scripted_responder())
    job = make_job(store)
    engine.plan(store.get_job(job["id"]))
    status = await engine.run_job(job["id"])

    assert status == "awaiting_approval"
    job = store.get_job(job["id"])
    assert job["status"] == "awaiting_approval"
    assert job["stage"] == "AWAITING_APPROVAL"
    roles = [t["role"] for t in store.tasks_for_job(job["id"])]
    assert "repository_analyst" in roles and "implementer" in roles
    assert "code_reviewer" in roles and "security_reviewer" in roles
    verifications = store.verifications_for_job(job["id"])
    assert verifications and verifications[-1]["passed"] is True
    assert verifications[-1]["exit_codes"] == [0]
    assert verifications[-1]["commit_sha"]
    commits = [a for a in store.artifacts_for_job(job["id"]) if a["kind"] == "commit"]
    assert commits
    pending = store.approvals_for_job(job["id"])
    assert pending and pending[0]["status"] == "pending"
    assert pending[0]["operation"] == "publish_branch_and_pr"
    store.close()


async def test_approval_then_resume_publishes_pr(env, origin, patch_worktree, patch_github):
    engine, store, client = make_engine(env, scripted_responder())
    job = make_job(store)
    engine.plan(store.get_job(job["id"]))
    await engine.run_job(job["id"])

    approval = store.approvals_for_job(job["id"])[0]
    store.decide_approval(approval["id"], "approved")

    status = await engine.run_job(job["id"])
    assert status == "published"
    job = store.get_job(job["id"])
    assert job["status"] == "published"
    assert job["stage"] == "PUBLISHED"
    pr_ops = [op for op in patch_github.operations if op[1].endswith("/pulls") and op[0] == "POST"]
    assert len(pr_ops) == 1
    assert patch_github.prs[1]["head"].startswith("fi-pr/")
    assert patch_github.prs[1]["base"] == "main"
    store.close()


async def test_publish_blocked_without_approval(env, origin, patch_worktree, patch_github):
    engine, store, client = make_engine(env, scripted_responder())
    job = make_job(store)
    engine.plan(store.get_job(job["id"]))
    await engine.run_job(job["id"])
    assert patch_github.operations == [], "no remote mutation before approval"
    store.close()


async def test_review_blocker_triggers_correction_cycle_then_blocked(env, origin, patch_worktree):
    engine, store, client = make_engine(env, scripted_responder(review_payload=BLOCKER_REVIEW))
    job = make_job(store)
    engine.plan(store.get_job(job["id"]))
    status = await engine.run_job(job["id"])

    assert status == "blocked"
    job = store.get_job(job["id"])
    assert job["status"] == "blocked"
    findings = store.findings_for_job(job["id"])
    assert any(f["title"] == "off-by-one" and f["severity"] == "high" for f in findings)
    store.close()


async def test_verification_failure_blocks_job(env, origin, patch_worktree):
    env = dict(env, FI_MAX_CORRECTION_CYCLES="0")
    responder = scripted_responder(verify_passed=False)
    engine, store, client = make_engine(env, responder)
    job = make_job(store)
    engine.plan(store.get_job(job["id"]))
    status = await engine.run_job(job["id"])
    assert status == "blocked"
    verifications = store.verifications_for_job(job["id"])
    assert verifications[-1]["passed"] is False
    store.close()


async def test_failing_test_command_blocks_job(env, origin, patch_worktree):
    env = dict(env, FI_MAX_CORRECTION_CYCLES="0")
    responder = scripted_responder(test_commands=("exit 3",))
    engine, store, client = make_engine(env, responder)
    job = make_job(store)
    engine.plan(store.get_job(job["id"]))
    status = await engine.run_job(job["id"])
    assert status == "blocked"
    verifications = store.verifications_for_job(job["id"])
    assert verifications[-1]["exit_codes"] == [3]
    store.close()


async def test_review_pr_job_runs_parallel_reviewers(env, origin, patch_worktree, patch_github):
    fetched = {}

    async def fake_github_get(ctx, kind, **kwargs):
        fetched[kind] = True
        return {"kind": kind, "title": "Add feature", "diff": "diff --git a/app.py b/app.py"}

    engine, store, client = make_engine(env, scripted_responder(publish=False))
    engine.registry._tools["github_get"].handler = fake_github_get

    job = make_job(store, kind="review_pr", pr_number=5)
    engine.plan(store.get_job(job["id"]))
    status = await engine.run_job(job["id"])

    assert status == "completed"
    assert fetched == {"pr": True, "pr_diff": True, "pr_files": True}
    job = store.get_job(job["id"])
    assert job["status"] == "completed"
    review_models = {c["model"] for c in client.calls
                     if "Independently review" in last_user_text(c["messages"])
                     or "Security review" in last_user_text(c["messages"])}
    assert len(review_models) >= 1
    store.close()


async def test_state_survives_restart(env, origin, patch_worktree, patch_github):
    engine, store, client = make_engine(env, scripted_responder())
    job = make_job(store)
    engine.plan(store.get_job(job["id"]))
    await engine.run_job(job["id"])
    approval_id = store.approvals_for_job(job["id"])[0]["id"]
    state_path = FIConfig(env).state_path
    store.close()

    reopened = Store(state_path)
    reopened.decide_approval(approval_id, "approved")
    engine2, store2, client2 = make_engine(env, scripted_responder())
    status = await engine2.run_job(job["id"])
    assert status == "published"
    events = reopened.events(job["id"])
    assert any(e["kind"] == "approval_decided" for e in events)
    store2.close()
    reopened.close()


async def test_worktree_edits_are_isolated(env, origin, patch_worktree):
    engine, store, client = make_engine(env, scripted_responder())
    job = make_job(store)
    engine.plan(store.get_job(job["id"]))
    await engine.run_job(job["id"])
    assert (origin / "app.py").read_text() == "VALUE = 1\n", "fixture origin must stay untouched"
    store.close()


async def test_publish_pushes_exact_verified_commit(env, origin, patch_worktree, patch_github):
    """The remote branch must point at the verified candidate commit itself —
    never a reconstructed API commit with a different SHA."""
    engine, store, client = make_engine(env, scripted_responder())
    job = make_job(store)
    engine.plan(store.get_job(job["id"]))
    await engine.run_job(job["id"])
    approval = store.approvals_for_job(job["id"])[0]
    store.decide_approval(approval["id"], "approved")
    status = await engine.run_job(job["id"])
    assert status == "published"

    commits = [a for a in store.artifacts_for_job(job["id"]) if a["kind"] == "commit"]
    verified_sha = commits[-1]["ref"]
    remote_branches = subprocess.run(
        ["git", "-C", str(origin), "for-each-ref", "--format=%(refname:short) %(objectname)",
         "refs/heads/"], capture_output=True, text=True).stdout.strip().splitlines()
    matching = [line for line in remote_branches if line.endswith(verified_sha)]
    assert matching, f"no remote branch at verified SHA {verified_sha}; got {remote_branches}"
    pr_ops = [op for op in patch_github.operations if op[1].endswith("/pulls") and op[0] == "POST"]
    assert len(pr_ops) == 1
    store.close()
