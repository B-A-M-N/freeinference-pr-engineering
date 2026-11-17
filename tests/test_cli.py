"""CLI tests: target parsing, parser wiring, offline commands."""

import json

import pytest

from freeinference_pr import cli
from freeinference_pr.config import FIConfig
from freeinference_pr.store import Store


def test_parse_target_pr_url():
    parsed = cli._parse_target("https://github.com/octo/example/pull/1781")
    assert parsed["kind"] == "review_pr"
    assert parsed["repository"] == "octo/example"
    assert parsed["pr_number"] == 1781


def test_parse_target_short_form():
    parsed = cli._parse_target("octo/example#42")
    assert parsed["kind"] == "review_pr"
    assert parsed["pr_number"] == 42


def test_parse_target_issue_url():
    parsed = cli._parse_target("https://github.com/octo/example/issues/7")
    assert parsed["repository"] == "octo/example"
    assert parsed["pr_number"] is None
    assert parsed["kind"] == "request"


def test_parse_target_natural_language():
    parsed = cli._parse_target("octo/example: add a retry helper")
    assert parsed["repository"] == "octo/example"
    assert parsed["kind"] == "request"
    assert "retry helper" in parsed["objective"]


def test_parser_builds_all_commands():
    parser = cli.build_parser()
    for argv in (
        ["submit", "octo/example#1"],
        ["resume", "job_x"],
        ["status"],
        ["status", "job_x"],
        ["approve", "appr_1", "--reject"],
        ["cancel", "job_x"],
        ["log", "job_x", "--limit", "5"],
        ["artifacts", "job_x"],
        ["doctor"],
        ["smoke"],
    ):
        args = parser.parse_args(argv)
        assert callable(args.func)


def test_status_command_on_empty_store(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FI_STATE_PATH", str(tmp_path / "state.db"))
    assert cli.main(["status"]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_status_detail_and_log_and_artifacts(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FI_STATE_PATH", str(tmp_path / "state.db"))
    store = Store(tmp_path / "state.db")
    job = store.create_job("request", "octo/example", "objective")
    store.add_finding(job["id"], "review", "bug", "high", ["a.py:1"], "detail")
    store.add_artifact(job["id"], "pr_draft", ref="body")
    store.log_event(job["id"], "system", "job_created", {})
    store.close()

    assert cli.main(["status", job["id"]]) == 0
    out = capsys.readouterr().out
    assert job["id"] in out and "bug" in out
    assert cli.main(["log", job["id"]]) == 0
    assert "job_created" in capsys.readouterr().out
    assert cli.main(["artifacts", job["id"]]) == 0
    assert "pr_draft" in capsys.readouterr().out


def test_submit_requires_api_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("FREEINFERENCE_API_KEY", raising=False)
    monkeypatch.setenv("FI_STATE_PATH", str(tmp_path / "state.db"))
    assert cli.main(["submit", "octo/example#1"]) == 2
    assert "FREEINFERENCE_API_KEY" in capsys.readouterr().err


def test_doctor_reports_missing_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("FREEINFERENCE_API_KEY", raising=False)
    monkeypatch.setenv("FI_STATE_PATH", str(tmp_path / "state.db"))
    assert cli.main(["doctor"]) == 1
    assert "FAIL" in capsys.readouterr().out
