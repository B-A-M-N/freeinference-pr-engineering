"""Runner bridge tests: role mapping, scope, SHA binding, idempotency, review."""

import json
import subprocess

import pytest

from freeinference_pr.config import FIConfig
from freeinference_pr.fi_provider import ChatResult, Usage
from freeinference_pr.runner_bridge import (
    RUNNER_MODE_TOOLS,
    RunnerBridgeError,
    RunnerTaskPackage,
    build_registry,
    build_review_packet,
    import_manual_review,
    load_package,
    path_allowed,
    run_runner_task,
)
from freeinference_pr.store import Store


@pytest.fixture()
def worktree(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()

    def git(*argv):
        subprocess.run(["git", "-C", str(wt), *argv], check=True, capture_output=True, text=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@e.c")
    git("config", "user.name", "T")
    (wt / "src.py").write_text("VALUE = 1\n")
    (wt / "other.py").write_text("X = 1\n")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    return wt


def make_pkg(worktree, role="implementer", **overrides):
    base_sha = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
    pkg = {
        "taskId": "task-1", "attemptId": "att-1", "attemptNo": 1, "projectId": "p",
        "repository": "octo/example", "role": role, "title": "bump", "goal": "bump VALUE",
        "instructions": "", "acceptance": ["VALUE == 2"], "nonGoals": [],
        "baseSha": base_sha, "branch": "runner/task-1", "worktree": str(worktree),
        "allowedPaths": ["src.py"], "forbiddenPaths": [], "maxMinutes": 5,
        "budgetUsd": 0.0, "evidence": [], "outputSchema": "", "stopBehavior": "",
        "idempotencyKey": "idem-1", "packageFile": "",
    }
    pkg.update(overrides)
    return RunnerTaskPackage.model_validate(pkg)


class FakeFI:
    def __init__(self, responder):
        self.responder = responder
        self.calls = 0

    async def chat(self, model, messages, tools=None, temperature=0.2, **kwargs):
        import asyncio

        self.calls += 1
        result = self.responder(model, messages)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    async def close(self):
        return None


def fj(payload):
    return ChatResult(content=json.dumps(payload), usage=Usage(2, 2, 4),
                      finish_reason="stop", model="fake")


def patch_client(monkeypatch, responder):
    fake = FakeFI(responder)
    monkeypatch.setattr("freeinference_pr.fi_provider.FreeInferenceClient", lambda config: fake)
    return fake


def make_config(tmp_path):
    return FIConfig({
        "FI_STATE_PATH": str(tmp_path / "state.db"),
        "FI_WORKSPACE_ROOT": str(tmp_path / "ws"),
        "FI_MAX_AGENT_TURNS": "4",
    })


def test_role_mapping_constants():
    assert RUNNER_MODE_TOOLS["verifier"] >= {"run_commands", "git_diff"}
    for role, tools in RUNNER_MODE_TOOLS.items():
        assert not any(name.startswith("github_") for name in tools), \
            "runner mode must have no GitHub tools"


def test_build_registry_strips_github_and_write_tools():
    for role in ("planner", "implementer", "verifier"):
        registry = build_registry(role)
        assert set(registry._tools) == RUNNER_MODE_TOOLS[role]


async def test_unsupported_role_fails_honestly(tmp_path, worktree):
    config = make_config(tmp_path)
    pkg = make_pkg(worktree, role="orchestrator")
    result = await run_runner_task(pkg, config)
    assert result["state"] == "failed"
    assert "not supported" in result["error"]


async def test_non_git_worktree_rejected(tmp_path):
    config = make_config(tmp_path)
    empty = tmp_path / "not-a-repo"
    empty.mkdir()
    pkg = make_pkg(empty)
    result = await run_runner_task(pkg, config)
    assert result["state"] == "failed"
    assert "not a git checkout" in result["error"]


async def test_sha_binding_refuses_stale_candidate(tmp_path, worktree, monkeypatch):
    fake = patch_client(monkeypatch, lambda m, ms: fj({}))
    config = make_config(tmp_path)
    pkg = make_pkg(worktree, baseSha="0" * 40)
    result = await run_runner_task(pkg, config)
    assert result["state"] == "failed"
    assert "candidate SHA moved" in result["error"]
    assert fake.calls == 0, "no inference may run against a stale candidate"


async def test_implementer_applies_scoped_edits_and_commits(tmp_path, worktree, monkeypatch):
    EDITS = {"edits": [{"path": "src.py", "content": "VALUE = 2\n"}],
             "commit_message": "bump", "regression_tests": [], "notes": ""}
    fake = patch_client(monkeypatch, lambda m, ms: fj(EDITS))
    config = make_config(tmp_path)
    result = await run_runner_task(make_pkg(worktree), config)
    assert result["state"] == "succeeded"
    assert result["commits"], "implementer must commit in Runner's worktree"
    assert (worktree / "src.py").read_text() == "VALUE = 2\n"
    assert result["head_sha"] == result["commits"][0]


async def test_edit_outside_allowed_paths_fails(tmp_path, worktree, monkeypatch):
    EDITS = {"edits": [{"path": "other.py", "content": "X = 2\n"}],
             "commit_message": "sneak", "regression_tests": [], "notes": ""}
    patch_client(monkeypatch, lambda m, ms: fj(EDITS))
    config = make_config(tmp_path)
    result = await run_runner_task(make_pkg(worktree), config)
    assert result["state"] == "failed"
    assert "outside allowed scope" in result["error"]
    assert (worktree / "other.py").read_text() == "X = 1\n"


async def test_forbidden_paths_block_edits(tmp_path, worktree, monkeypatch):
    EDITS = {"edits": [{"path": "src.py", "content": "VALUE = 3\n"}],
             "commit_message": "m", "regression_tests": [], "notes": ""}
    patch_client(monkeypatch, lambda m, ms: fj(EDITS))
    config = make_config(tmp_path)
    pkg = make_pkg(worktree, allowedPaths=[], forbiddenPaths=["src.py"])
    result = await run_runner_task(pkg, config)
    assert result["state"] == "failed"
    assert "outside allowed scope" in result["error"]


async def test_idempotency_replay_does_not_rerun(tmp_path, worktree, monkeypatch):
    EDITS = {"edits": [{"path": "src.py", "content": "VALUE = 2\n"}],
             "commit_message": "bump", "regression_tests": [], "notes": ""}
    fake = patch_client(monkeypatch, lambda m, ms: fj(EDITS))
    config = make_config(tmp_path)
    first = await run_runner_task(make_pkg(worktree), config)
    assert first["state"] == "succeeded"
    second = await run_runner_task(make_pkg(worktree), config)
    assert second["replayed"] is True
    assert second["head_sha"] == first["head_sha"]
    assert fake.calls == 1, "replayed package must not re-run inference"


async def test_verifier_produces_runner_compatible_verdict(tmp_path, worktree, monkeypatch):
    REVIEW = {"assessment": "ok", "findings": [], "missed_paths": [],
              "unsupported_assumptions": []}
    patch_client(monkeypatch, lambda m, ms: fj(REVIEW))
    config = make_config(tmp_path)
    pkg = make_pkg(worktree, role="verifier")
    result = await run_runner_task(pkg, config)
    assert result["state"] == "succeeded"
    verdict = json.loads(result["result"])["verdict"]
    assert verdict["decision"] == "approve"
    assert verdict["candidate_sha"] == result["head_sha"]
    assert len(verdict["diff_digest"]) == 64
    assert verdict["evidence_kind"] == "fi_review"


async def test_verifier_rejects_on_blocker_finding(tmp_path, worktree, monkeypatch):
    REVIEW = {"assessment": "bad", "findings": [{"title": "injection", "severity": "critical",
                                                 "evidence": ["src.py:1"], "detail": "d"}],
              "missed_paths": [], "unsupported_assumptions": []}
    patch_client(monkeypatch, lambda m, ms: fj(REVIEW))
    config = make_config(tmp_path)
    result = await run_runner_task(make_pkg(worktree, role="verifier"), config)
    verdict = json.loads(result["result"])["verdict"]
    assert verdict["decision"] == "reject"
    assert verdict["findings"][0]["severity"] == "critical"


async def test_cancellation_reports_cancelled(tmp_path, worktree, monkeypatch):
    import asyncio

    async def slow_responder(model, messages):
        await asyncio.sleep(30)
        return fj({})

    patch_client(monkeypatch, slow_responder)
    config = make_config(tmp_path)
    task = asyncio.create_task(run_runner_task(make_pkg(worktree), config))
    await asyncio.sleep(0.1)
    task.cancel()
    result = await task
    assert result["state"] == "cancelled"


async def test_runner_job_state_persisted_for_restart(tmp_path, worktree, monkeypatch):
    EDITS = {"edits": [{"path": "src.py", "content": "VALUE = 2\n"}],
             "commit_message": "bump", "regression_tests": [], "notes": ""}
    patch_client(monkeypatch, lambda m, ms: fj(EDITS))
    config = make_config(tmp_path)
    await run_runner_task(make_pkg(worktree), config)
    store = Store(config.state_path)
    jobs = store.list_jobs()
    assert jobs and jobs[0]["kind"] == "runner_task"
    mapping = store.idempotency_get("runner-job:idem-1")
    assert mapping and mapping["job_id"] == jobs[0]["id"]
    events = store.events(jobs[0]["id"])
    assert any(e["kind"] == "runner_task_result" for e in events)
    store.close()


def test_path_allowed_globs():
    assert path_allowed("src/a.py", ["src/*"], [])
    assert not path_allowed("docs/x.md", ["src/*"], [])
    assert not path_allowed("src/secrets.py", ["src/*"], ["src/secrets*"])
    assert path_allowed("anything.txt", [], []), "empty allowed list permits all"
    assert not path_allowed("../escape.py", ["*"], [])


def test_load_package_roundtrip(tmp_path):
    pkg = {
        "taskId": "t", "attemptId": "a", "role": "planner", "worktree": "/tmp/wt",
    }
    path = tmp_path / "package.json"
    path.write_text(json.dumps(pkg))
    loaded = load_package(path)
    assert loaded.role == "planner"
    assert loaded.taskId == "t"


def test_review_packet_contains_required_elements():
    packet = build_review_packet(
        repository="octo/example", task_identity="task-1 attempt 2",
        candidate_sha="abc123", base_sha="def456", objective="bump VALUE",
        acceptance=["VALUE == 2"], diff="diff --git a/src.py b/src.py\n+VALUE = 2",
        test_results=[{"command": "pytest", "exit_code": 0}],
        known_risks=["touches constant"], review_questions=["Any missed path?"],
        reviewer_hint="kontrol rendezvous",
    )
    for required in ("octo/example", "abc123", "def456", "VALUE == 2", "diff --git",
                     "pytest", "exit 0", "Known risks", "Review questions",
                     '"decision"', "candidate_sha"):
        assert required in packet, f"packet missing: {required}"


def test_import_manual_review_records_operator_evidence(tmp_path):
    store = Store(tmp_path / "s.db")
    job = store.create_job("request", "octo/example", "obj")
    verdict = json.dumps({
        "decision": "changes_requested", "candidate_sha": "abc123",
        "findings": [{"title": "missing test", "severity": "medium",
                      "evidence": ["src.py"], "detail": "add one"}],
        "reviewer": "chatgpt-session", "reviewed_at": "2026-01-01",
    })
    record = import_manual_review(store, job["id"], verdict,
                                  candidate_sha="abc123", reviewer="op",
                                  transport="kontrol")
    assert record["source"] == "operator"
    assert record["decision"] == "changes_requested"
    artifacts = store.artifacts_for_job(job["id"])
    assert any(a["kind"] == "external_review" for a in artifacts)
    findings = store.findings_for_job(job["id"])
    assert findings and findings[0]["title"] == "missing test"
    assert findings[0]["source"] == "external_review:kontrol"
    events = store.events(job["id"])
    assert any("not an authenticated automated response" in e["payload_json"]
               for e in events if e["kind"] == "external_review_recorded")
    store.close()


def test_import_manual_review_rejects_stale_candidate(tmp_path):
    store = Store(tmp_path / "s.db")
    job = store.create_job("request", "octo/example", "obj")
    verdict = json.dumps({"decision": "approve", "candidate_sha": "old123"})
    with pytest.raises(RunnerBridgeError, match="stale reviews are rejected"):
        import_manual_review(store, job["id"], verdict,
                             candidate_sha="new456", reviewer="op", transport="manual")
    store.close()


def test_import_manual_review_rejects_bad_decision(tmp_path):
    store = Store(tmp_path / "s.db")
    job = store.create_job("request", "octo/example", "obj")
    with pytest.raises(RunnerBridgeError, match="decision"):
        import_manual_review(store, job["id"], '{"decision": "ship it"}',
                             candidate_sha="abc", reviewer="op", transport="manual")
    with pytest.raises(RunnerBridgeError, match="no JSON verdict"):
        import_manual_review(store, job["id"], "looks great to me",
                             candidate_sha="abc", reviewer="op", transport="manual")
    store.close()
