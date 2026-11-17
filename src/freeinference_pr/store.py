"""SQLite-backed durable state: jobs, tasks, findings, verifications, approvals.

WAL-mode SQLite is sufficient for a single-user deployment. The store is the
recovery source of truth: every state transition is written before the effect it
describes is attempted, an append-only event log records tool actions and agent
results (redacted), and `recover()` classifies interrupted work after a process
restart. Idempotency keys make branch creation, commits, pushes, and PR
creation safe to retry.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from freeinference_pr.redaction import redact_obj

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    repository TEXT NOT NULL,
    pr_number INTEGER,
    issue_number INTEGER,
    objective TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    stage TEXT NOT NULL DEFAULT 'INTAKE',
    plan_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    role TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    depends_on TEXT NOT NULL DEFAULT '[]',
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    result_json TEXT,
    error TEXT,
    input_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    heartbeat_at REAL
);
CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    source TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'OPEN',
    title TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    detail TEXT NOT NULL DEFAULT '',
    correction_task_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS verifications (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    task_id TEXT,
    commands_json TEXT NOT NULL DEFAULT '[]',
    exit_codes_json TEXT NOT NULL DEFAULT '[]',
    passed INTEGER NOT NULL DEFAULT 0,
    commit_sha TEXT,
    notes TEXT NOT NULL DEFAULT '',
    stale INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    operation TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    requested_at REAL NOT NULL,
    decided_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    kind TEXT NOT NULL,
    path TEXT,
    ref TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_job ON tasks(job_id);
CREATE INDEX IF NOT EXISTS idx_findings_job ON findings(job_id);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id);
"""

JOB_TERMINAL = {"completed", "published", "blocked", "failed", "cancelled"}


def now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ helpers

    def _execute(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        cursor = self._conn.execute(sql, params)
        self._conn.commit()
        return cursor.fetchall()

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self._execute(sql, params)
        return rows[0] if rows else None

    @staticmethod
    def _dumps(value: Any) -> str:
        return json.dumps(redact_obj(value), default=str)

    @staticmethod
    def _loads(raw: str | None, default: Any) -> Any:
        if not raw:
            return default
        return json.loads(raw)

    # ------------------------------------------------------------------ events

    def log_event(self, job_id: str, actor: str, kind: str, payload: dict[str, Any] | None = None) -> None:
        self._execute(
            "INSERT INTO events (job_id, ts, actor, kind, payload_json) VALUES (?,?,?,?,?)",
            (job_id, now(), actor, kind, self._dumps(payload or {})),
        )

    def events(self, job_id: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._execute(
            "SELECT * FROM events WHERE job_id=? ORDER BY id DESC LIMIT ?", (job_id, limit)
        )
        return [dict(row) for row in reversed(rows)]

    # ------------------------------------------------------------------ jobs

    def create_job(
        self, kind: str, repository: str, objective: str,
        pr_number: int | None = None, issue_number: int | None = None,
    ) -> dict[str, Any]:
        job_id = new_id("job")
        stamp = now()
        self._execute(
            "INSERT INTO jobs (id, kind, repository, pr_number, issue_number, objective, status, stage, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (job_id, kind, repository, pr_number, issue_number, objective, "open", "INTAKE", stamp, stamp),
        )
        self.log_event(job_id, "system", "job_created", {"kind": kind, "repository": repository})
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        row = self._one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if row is None:
            raise KeyError(f"job not found: {job_id}")
        return dict(row)

    def update_job(self, job_id: str, **fields: Any) -> None:
        allowed = {"status", "stage", "plan_json", "objective"}
        sets, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"cannot update job field {key}")
            sets.append(f"{key}=?")
            params.append(value)
        sets.append("updated_at=?")
        params.append(now())
        params.append(job_id)
        self._execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", tuple(params))

    def list_jobs(self, statuses: list[str] | None = None) -> list[dict[str, Any]]:
        if statuses:
            marks = ",".join("?" for _ in statuses)
            rows = self._execute(f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY created_at DESC", tuple(statuses))
        else:
            rows = self._execute("SELECT * FROM jobs ORDER BY created_at DESC")
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ tasks

    def add_task(
        self, job_id: str, role: str, title: str,
        depends_on: list[str] | None = None, input_data: dict[str, Any] | None = None,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        task_id = new_id("task")
        self._execute(
            "INSERT INTO tasks (id, job_id, role, title, depends_on, input_json, max_attempts, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (task_id, job_id, role, title, self._dumps(depends_on or []), self._dumps(input_data or {}), max_attempts, now()),
        )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict[str, Any]:
        row = self._one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if row is None:
            raise KeyError(f"task not found: {task_id}")
        return dict(row)

    def tasks_for_job(self, job_id: str) -> list[dict[str, Any]]:
        rows = self._execute("SELECT * FROM tasks WHERE job_id=? ORDER BY created_at", (job_id,))
        result = []
        for row in rows:
            task = dict(row)
            task["depends_on"] = self._loads(task["depends_on"], [])
            result.append(task)
        return result

    def update_task(self, task_id: str, **fields: Any) -> None:
        allowed = {"status", "attempt", "result_json", "error", "started_at", "finished_at", "heartbeat_at", "input_json"}
        sets, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"cannot update task field {key}")
            sets.append(f"{key}=?")
            params.append(value)
        params.append(task_id)
        self._execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", tuple(params))

    def heartbeat(self, task_id: str) -> None:
        self.update_task(task_id, heartbeat_at=now())

    def ready_tasks(self, job_id: str) -> list[dict[str, Any]]:
        """Pending tasks whose dependencies are all done."""
        tasks = self.tasks_for_job(job_id)
        by_id = {t["id"]: t for t in tasks}
        ready = []
        for task in tasks:
            if task["status"] != "pending":
                continue
            deps = task["depends_on"] or []
            if all(by_id.get(dep, {}).get("status") == "done" for dep in deps):
                ready.append(task)
        return ready

    # ------------------------------------------------------------------ findings

    def add_finding(
        self, job_id: str, source: str, title: str, severity: str = "medium",
        evidence: list[str] | None = None, detail: str = "",
    ) -> dict[str, Any]:
        existing = self._one(
            "SELECT id FROM findings WHERE job_id=? AND lower(title)=lower(?)", (job_id, title.strip())
        )
        if existing:
            return dict(self._one("SELECT * FROM findings WHERE id=?", (existing["id"],)))
        finding_id = new_id("find")
        stamp = now()
        self._execute(
            "INSERT INTO findings (id, job_id, source, severity, status, title, evidence_json, detail, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (finding_id, job_id, source, severity, "OPEN", title.strip(), self._dumps(evidence or []), detail, stamp, stamp),
        )
        return dict(self._one("SELECT * FROM findings WHERE id=?", (finding_id,)))

    def findings_for_job(self, job_id: str, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self._execute(
                "SELECT * FROM findings WHERE job_id=? AND status=? ORDER BY created_at", (job_id, status)
            )
        else:
            rows = self._execute("SELECT * FROM findings WHERE job_id=? ORDER BY created_at", (job_id,))
        result = []
        for row in rows:
            finding = dict(row)
            finding["evidence"] = self._loads(finding.pop("evidence_json"), [])
            result.append(finding)
        return result

    def update_finding(self, finding_id: str, **fields: Any) -> None:
        allowed = {"status", "correction_task_id", "severity", "detail"}
        sets, params = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"cannot update finding field {key}")
            sets.append(f"{key}=?")
            params.append(value)
        sets.append("updated_at=?")
        params.append(now())
        params.append(finding_id)
        self._execute(f"UPDATE findings SET {', '.join(sets)} WHERE id=?", tuple(params))

    # ------------------------------------------------------------------ verifications

    def add_verification(
        self, job_id: str, task_id: str | None, commands: list[str], exit_codes: list[int],
        passed: bool, commit_sha: str | None = None, notes: str = "",
    ) -> dict[str, Any]:
        verification_id = new_id("verif")
        self._execute(
            "INSERT INTO verifications (id, job_id, task_id, commands_json, exit_codes_json, passed, commit_sha, notes, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (verification_id, job_id, task_id, self._dumps(commands), self._dumps(exit_codes),
             int(passed), commit_sha, notes, now()),
        )
        return dict(self._one("SELECT * FROM verifications WHERE id=?", (verification_id,)))

    def verifications_for_job(self, job_id: str) -> list[dict[str, Any]]:
        rows = self._execute("SELECT * FROM verifications WHERE job_id=? ORDER BY created_at", (job_id,))
        result = []
        for row in rows:
            verification = dict(row)
            verification["commands"] = self._loads(verification.pop("commands_json"), [])
            verification["exit_codes"] = self._loads(verification.pop("exit_codes_json"), [])
            verification["passed"] = bool(verification["passed"])
            verification["stale"] = bool(verification["stale"])
            result.append(verification)
        return result

    def invalidate_verifications(self, job_id: str) -> None:
        self._execute("UPDATE verifications SET stale=1 WHERE job_id=?", (job_id,))

    # ------------------------------------------------------------------ approvals

    def request_approval(self, job_id: str, operation: str, details: dict[str, Any]) -> dict[str, Any]:
        pending = self._one(
            "SELECT * FROM approvals WHERE job_id=? AND operation=? AND status='pending'",
            (job_id, operation),
        )
        if pending:
            approval = dict(pending)
            approval["details"] = self._loads(approval.pop("details_json"), {})
            return approval
        approval_id = new_id("appr")
        self._execute(
            "INSERT INTO approvals (id, job_id, operation, details_json, status, requested_at)"
            " VALUES (?,?,?,?,?,?)",
            (approval_id, job_id, operation, self._dumps(details), "pending", now()),
        )
        self.log_event(job_id, "system", "approval_requested", {"operation": operation, **details})
        row = self._one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        approval = dict(row)
        approval["details"] = self._loads(approval.pop("details_json"), {})
        return approval

    def decide_approval(self, approval_id: str, decision: str) -> dict[str, Any]:
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be 'approved' or 'rejected'")
        self._execute(
            "UPDATE approvals SET status=?, decided_at=? WHERE id=?", (decision, now(), approval_id)
        )
        row = self._one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        approval = dict(row)
        approval["details"] = self._loads(approval.pop("details_json"), {})
        if approval["job_id"]:
            self.log_event(approval["job_id"], "user", "approval_decided",
                           {"operation": approval["operation"], "decision": decision})
        return approval

    def approvals_for_job(self, job_id: str) -> list[dict[str, Any]]:
        rows = self._execute("SELECT * FROM approvals WHERE job_id=? ORDER BY requested_at", (job_id,))
        result = []
        for row in rows:
            approval = dict(row)
            approval["details"] = self._loads(approval.pop("details_json"), {})
            result.append(approval)
        return result

    # ------------------------------------------------------------------ artifacts & idempotency

    def add_artifact(self, job_id: str, kind: str, path: str | None = None, ref: str | None = None) -> str:
        artifact_id = new_id("art")
        self._execute(
            "INSERT INTO artifacts (id, job_id, kind, path, ref, created_at) VALUES (?,?,?,?,?,?)",
            (artifact_id, job_id, kind, path, ref, now()),
        )
        return artifact_id

    def artifacts_for_job(self, job_id: str) -> list[dict[str, Any]]:
        rows = self._execute("SELECT * FROM artifacts WHERE job_id=? ORDER BY created_at", (job_id,))
        return [dict(row) for row in rows]

    def idempotency_get(self, key: str) -> Any:
        row = self._one("SELECT value FROM kv WHERE key=?", (key,))
        return self._loads(row["value"], None) if row else None

    def idempotency_put(self, key: str, value: Any) -> None:
        self._execute(
            "INSERT INTO kv (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, self._dumps(value)),
        )

    # ------------------------------------------------------------------ recovery

    def recover(self, stale_seconds: float = 600.0) -> dict[str, Any]:
        """Classify interrupted work after a restart. Returns a summary.

        Tasks marked 'running' whose heartbeat is stale (or absent) are marked
        'interrupted'; their jobs become 'interrupted' and can be resumed by the
        scheduler (done tasks are never re-run).
        """
        cutoff = now() - stale_seconds
        rows = self._execute("SELECT * FROM tasks WHERE status='running'")
        interrupted: list[str] = []
        for row in rows:
            task = dict(row)
            heartbeat = task["heartbeat_at"] or task["started_at"] or 0.0
            if heartbeat < cutoff:
                self.update_task(task["id"], status="interrupted", error="process restarted mid-task")
                interrupted.append(task["id"])
                self.log_event(task["job_id"], "system", "task_interrupted", {"task_id": task["id"]})
        job_rows = self._execute(
            "SELECT DISTINCT job_id FROM tasks WHERE status='interrupted'"
        )
        resumed_jobs = []
        for row in job_rows:
            job_id = row["job_id"]
            self.update_job(job_id, status="interrupted")
            resumed_jobs.append(job_id)
        return {"interrupted_tasks": interrupted, "interrupted_jobs": resumed_jobs}
