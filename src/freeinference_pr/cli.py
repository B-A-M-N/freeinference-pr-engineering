"""Command-line interface for the independent FreeInference PR engineering system.

Runs entirely without Mistral Cloud: state in SQLite, inference on
FreeInference, GitHub over the REST API with a least-privilege token.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

from freeinference_pr.config import FIConfig
from freeinference_pr.engine import Engine
from freeinference_pr.fi_provider import FreeInferenceClient, ProviderError
from freeinference_pr.redaction import redact
from freeinference_pr.store import Store
from freeinference_pr.tools import ToolRegistry


def _parse_target(text: str) -> dict:
    text = text.strip()
    match = re.search(
        r"(?:https?://github\.com/)?([\w.-]+/[\w.-]+)"
        r"(?:/pull/(\d+)|/issues/(\d+))?"
        r"(?:#(pull/)?(\d+))?",
        text,
    )
    if not match:
        return {"kind": "request", "repository": "", "objective": text}
    repository = match.group(1)
    pr_number = int(match.group(2) or match.group(5) or 0) or None
    issue_number = int(match.group(3) or 0) or None
    kind = "review_pr" if pr_number else "request"
    objective = text
    return {"kind": kind, "repository": repository, "pr_number": pr_number,
            "issue_number": issue_number, "objective": objective}


def _make_engine(config: FIConfig, store: Store, client: FreeInferenceClient) -> Engine:
    return Engine(config, store, client, ToolRegistry())


def _print_jobs(rows: list[dict]) -> None:
    for job in rows:
        print(f"{job['id']}  {job['status']:<18} {job['stage']:<18} {job['kind']:<12} {job['repository']}"
              + (f"#{job['pr_number']}" if job.get("pr_number") else ""))


def _print_tasks(tasks: list[dict]) -> None:
    for task in tasks:
        print(f"{task['id']}  {task['status']:<12} {task['role']:<20} {task['title'][:70]}"
              + (f"  attempt={task['attempt']}" if task["attempt"] else "")
              + (f"  error={task['error'][:60]}" if task.get("error") else ""))


async def cmd_submit(args: argparse.Namespace) -> int:
    config = FIConfig()
    if not config.api_key:
        print("error: FREEINFERENCE_API_KEY is not set", file=sys.stderr)
        return 2
    store = Store(config.state_path)
    target = _parse_target(args.target)
    if not target["repository"]:
        print("error: could not parse a repository (owner/repo) from the target", file=sys.stderr)
        return 2
    objective = args.objective or target["objective"]
    job = store.create_job(target["kind"], target["repository"], objective,
                           pr_number=target.get("pr_number"),
                           issue_number=target.get("issue_number"))
    engine = _make_engine(config, store, FreeInferenceClient(config))
    try:
        if not args.dry_run:
            engine.plan(job)
            status = await engine.run_job(job["id"])
            print(f"job {job['id']} -> {status}")
        else:
            print(f"job {job['id']} created (dry run; plan not built)")
    finally:
        await engine.client.close()
        store.close()
    return 0


async def cmd_resume(args: argparse.Namespace) -> int:
    config = FIConfig()
    store = Store(config.state_path)
    job = store.get_job(args.job_id)
    engine = _make_engine(config, store, FreeInferenceClient(config))
    try:
        status = await engine.run_job(job["id"])
        print(f"job {job['id']} -> {status}")
    finally:
        await engine.client.close()
        store.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = FIConfig()
    store = Store(config.state_path)
    try:
        if args.job_id:
            job = store.get_job(args.job_id)
            print(json.dumps({**job, "plan": json.loads(job["plan_json"] or "[]")}, indent=2, default=str))
            print("\ntasks:")
            _print_tasks(store.tasks_for_job(args.job_id))
            print("\nfindings:")
            for finding in store.findings_for_job(args.job_id):
                print(f"  {finding['id']} [{finding['severity']}] {finding['status']:<10} {finding['title']}")
            print("\nverifications:")
            for verification in store.verifications_for_job(args.job_id):
                print(f"  {verification['id']} passed={verification['passed']} stale={verification['stale']} "
                      f"exit_codes={verification['exit_codes']} commit={verification.get('commit_sha')}")
            print("\napprovals:")
            for approval in store.approvals_for_job(args.job_id):
                print(f"  {approval['id']} {approval['status']:<10} {approval['operation']} {approval['details']}")
            print("\nartifacts:")
            for artifact in store.artifacts_for_job(args.job_id):
                print(f"  {artifact['id']} {artifact['kind']}")
        else:
            _print_jobs(store.list_jobs())
    finally:
        store.close()
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    config = FIConfig()
    store = Store(config.state_path)
    try:
        approval = store.decide_approval(args.approval_id, "approved" if not args.reject else "rejected")
        print(f"approval {approval['id']} -> {approval['status']}")
        if approval["status"] == "approved":
            print(f"resume with: fi-pr resume {approval['job_id']}")
    finally:
        store.close()
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    config = FIConfig()
    store = Store(config.state_path)
    try:
        store.update_job(args.job_id, status="cancelled")
        store.log_event(args.job_id, "user", "job_cancelled", {})
        print(f"job {args.job_id} cancelled")
    finally:
        store.close()
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    config = FIConfig()
    store = Store(config.state_path)
    try:
        for event in store.events(args.job_id, limit=args.limit):
            print(f"{event['ts']:.0f} {event['actor']:<20} {event['kind']:<24} {redact(event['payload_json'])[:160]}")
    finally:
        store.close()
    return 0


def cmd_artifacts(args: argparse.Namespace) -> int:
    config = FIConfig()
    store = Store(config.state_path)
    try:
        for artifact in store.artifacts_for_job(args.job_id):
            print(f"== {artifact['id']} {artifact['kind']} ==")
            print(redact(artifact.get("ref") or artifact.get("path") or ""))
    finally:
        store.close()
    return 0


async def cmd_doctor(args: argparse.Namespace) -> int:
    config = FIConfig()
    checks: list[tuple[str, bool, str]] = []
    checks.append(("FREEINFERENCE_API_KEY set", bool(config.api_key),
                   "export FREEINFERENCE_API_KEY=hyi-..." if not config.api_key else ""))
    try:
        async with FreeInferenceClient(config) as client:
            models = await client.list_models()
        checks.append(("FreeInference reachable", True, f"{len(models)} models: {sorted(models)[:5]}"))
        orchestrator_model = config.model_for("orchestrator")
        if models and orchestrator_model not in models:
            checks.append(("orchestrator model in catalog", False,
                           f"{orchestrator_model} not in {sorted(models)}"))
        else:
            checks.append(("orchestrator model in catalog", True, orchestrator_model))
    except ProviderError as exc:
        checks.append(("FreeInference reachable", False, str(exc)))
    import shutil

    checks.append(("git available", shutil.which("git") is not None, "git required for worktrees"))
    checks.append(("GITHUB_TOKEN set", bool(config.github_token),
                   "needed for GitHub reads/writes (least-privilege token)"))
    ok = True
    for name, passed, note in checks:
        ok = ok and passed
        print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" — {note}" if note else ""))
    return 0 if ok else 1


async def cmd_smoke(args: argparse.Namespace) -> int:
    from freeinference_pr.smoke import run_smoke

    return await run_smoke(FIConfig())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fi-pr", description="FreeInference PR engineering system")
    sub = parser.add_subparsers(dest="command", required=True)

    submit = sub.add_parser("submit", help="submit a repository/PR/issue/natural-language task")
    submit.add_argument("target", help="e.g. owner/repo#123, a PR URL, or 'owner/repo: do X'")
    submit.add_argument("-o", "--objective", help="explicit objective text")
    submit.add_argument("--dry-run", action="store_true", help="create the job without running it")
    submit.set_defaults(func=cmd_submit)

    resume = sub.add_parser("resume", help="resume/continue a job (after approval or interruption)")
    resume.add_argument("job_id")
    resume.set_defaults(func=cmd_resume)

    status = sub.add_parser("status", help="list jobs or show one job in detail")
    status.add_argument("job_id", nargs="?")
    status.set_defaults(func=cmd_status)

    approve = sub.add_parser("approve", help="approve or reject a pending approval")
    approve.add_argument("approval_id")
    approve.add_argument("--reject", action="store_true")
    approve.set_defaults(func=cmd_approve)

    cancel = sub.add_parser("cancel", help="cancel a job")
    cancel.add_argument("job_id")
    cancel.set_defaults(func=cmd_cancel)

    log = sub.add_parser("log", help="show a job's event log")
    log.add_argument("job_id")
    log.add_argument("--limit", type=int, default=100)
    log.set_defaults(func=cmd_log)

    artifacts = sub.add_parser("artifacts", help="show a job's artifacts (drafts, diffs, commits)")
    artifacts.add_argument("job_id")
    artifacts.set_defaults(func=cmd_artifacts)

    doctor = sub.add_parser("doctor", help="runtime health checks")
    doctor.set_defaults(func=cmd_doctor)

    smoke = sub.add_parser("smoke", help="real FreeInference integration smoke test")
    smoke.set_defaults(func=cmd_smoke)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    coro = getattr(args, "func", None)
    import inspect

    result = coro(args)
    if inspect.iscoroutine(result):
        return asyncio.run(result)
    return result


if __name__ == "__main__":
    sys.exit(main())
