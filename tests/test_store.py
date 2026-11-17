"""Store tests: persistence across restart, recovery, dedupe, approvals."""

import time

import pytest

from freeinference_pr.store import Store


def make_store(tmp_path):
    return Store(tmp_path / "state.db")


def test_persistence_across_store_instances(tmp_path):
    store = make_store(tmp_path)
    job = store.create_job("request", "owner/repo", "fix the bug")
    store.add_task(job["id"], "implementer", "implement")
    store.add_finding(job["id"], "review", "off-by-one", "high", ["a.py:2"], "detail")
    store.add_verification(job["id"], None, ["pytest"], [0], True, commit_sha="abc1234")
    store.add_artifact(job["id"], "pr_draft", ref="body")
    store.idempotency_put("worktree:x", {"path": "/tmp/wt"})
    store.close()

    reopened = make_store(tmp_path)
    persisted = reopened.get_job(job["id"])
    assert persisted["status"] == "open"
    assert [t["role"] for t in reopened.tasks_for_job(job["id"])] == ["implementer"]
    findings = reopened.findings_for_job(job["id"])
    assert findings[0]["title"] == "off-by-one"
    assert findings[0]["evidence"] == ["a.py:2"]
    verifications = reopened.verifications_for_job(job["id"])
    assert verifications[0]["passed"] is True
    assert verifications[0]["commit_sha"] == "abc1234"
    assert reopened.artifacts_for_job(job["id"])[0]["kind"] == "pr_draft"
    assert reopened.idempotency_get("worktree:x") == {"path": "/tmp/wt"}
    reopened.close()


def test_finding_dedupe_by_title(tmp_path):
    store = make_store(tmp_path)
    job = store.create_job("request", "owner/repo", "objective")
    first = store.add_finding(job["id"], "review", "SQL Injection", "critical", ["a:1"])
    second = store.add_finding(job["id"], "code_review", "sql injection", "critical", ["b:2"])
    assert first["id"] == second["id"]
    assert len(store.findings_for_job(job["id"])) == 1
    store.close()


def test_ready_tasks_respects_dependencies(tmp_path):
    store = make_store(tmp_path)
    job = store.create_job("request", "owner/repo", "objective")
    analyst = store.add_task(job["id"], "repository_analyst", "analyze")
    implementer = store.add_task(job["id"], "implementer", "implement", [analyst["id"]])
    assert [t["id"] for t in store.ready_tasks(job["id"])] == [analyst["id"]]
    store.update_task(analyst["id"], status="done")
    assert [t["id"] for t in store.ready_tasks(job["id"])] == [implementer["id"]]
    store.close()


def test_approval_flow_and_reuse_of_pending(tmp_path):
    store = make_store(tmp_path)
    job = store.create_job("request", "owner/repo", "objective")
    first = store.request_approval(job["id"], "publish_branch_and_pr", {"branch": "fi-pr/x"})
    again = store.request_approval(job["id"], "publish_branch_and_pr", {"branch": "fi-pr/x"})
    assert first["id"] == again["id"]
    assert first["status"] == "pending"

    decided = store.decide_approval(first["id"], "approved")
    assert decided["status"] == "approved"
    events = store.events(job["id"])
    assert any(e["kind"] == "approval_decided" for e in events)

    rejected = store.request_approval(job["id"], "pr_comment", {"pr_number": 5})
    assert store.decide_approval(rejected["id"], "rejected")["status"] == "rejected"
    with pytest.raises(ValueError):
        store.decide_approval(rejected["id"], "maybe")
    store.close()


def test_recover_marks_stale_running_tasks_interrupted(tmp_path):
    store = make_store(tmp_path)
    job = store.create_job("request", "owner/repo", "objective")
    stale = store.add_task(job["id"], "implementer", "stale work")
    fresh = store.add_task(job["id"], "verifier", "fresh work")
    store.update_task(stale["id"], status="running", started_at=time.time() - 3600,
                      heartbeat_at=time.time() - 3600)
    store.update_task(fresh["id"], status="running", started_at=time.time(),
                      heartbeat_at=time.time())

    summary = store.recover(stale_seconds=600.0)
    assert summary["interrupted_tasks"] == [stale["id"]]
    assert store.get_task(stale["id"])["status"] == "interrupted"
    assert store.get_task(fresh["id"])["status"] == "running"
    assert store.get_job(job["id"])["status"] == "interrupted"
    store.close()


def test_done_tasks_survive_recovery(tmp_path):
    store = make_store(tmp_path)
    job = store.create_job("request", "owner/repo", "objective")
    done = store.add_task(job["id"], "repository_analyst", "analysis")
    store.update_task(done["id"], status="done")
    summary = store.recover(stale_seconds=0.0)
    assert summary["interrupted_tasks"] == []
    assert store.get_task(done["id"])["status"] == "done"
    store.close()


def test_events_are_redacted(tmp_path):
    store = make_store(tmp_path)
    job = store.create_job("request", "owner/repo", "objective")
    store.log_event(job["id"], "tool", "github_call", {"url": "https://x", "auth": "Bearer hyi-secretvalue12345"})
    events = store.events(job["id"])
    assert "hyi-secretvalue12345" not in events[-1]["payload_json"]
    assert "[REDACTED]" in events[-1]["payload_json"]
    store.close()


def test_update_rejects_unknown_fields(tmp_path):
    store = make_store(tmp_path)
    job = store.create_job("request", "owner/repo", "objective")
    with pytest.raises(ValueError):
        store.update_job(job["id"], pr_number=99)
    store.close()
