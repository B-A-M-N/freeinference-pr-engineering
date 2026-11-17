"""ACP server mode — expose PR engineering missions to any ACP harness.

Implements the agent side of the Agent Client Protocol (newline-delimited
JSON-RPC 2.0 over stdio; contract verified against Kontrol's
``src/acp-duplex.ts`` and ``scripts/qualify-acp-harness.mjs``):

    initialize                       -> {protocolVersion, agentCapabilities, serverInfo}
    session/new  {cwd, mcpServers}   -> {sessionId}
    session/prompt {sessionId, prompt:[{type:"text", text}]} -> {stopReason}
    session/load {sessionId}         -> {sessionId, jobs: [...]}
    session/update (notification)    -> streaming progress (agent_message_chunk)

Prompt commands (first line wins, case-insensitive):
    status [job_id]        mission status summary (all jobs when omitted)
    approve <approval_id>  record an operator approval decision
    reject <approval_id>   record an operator rejection
    cancel <job_id>        cancel a job
    anything else          submitted as a new work request (natural language
                           or owner/repo reference); the engine plans and runs
                           it through the configured inference backend.

Security posture (unchanged from the rest of the system):
- No fs/* or terminal/* support: agent-initiated client calls are answered
  with JSON-RPC method-not-found (fail closed).
- Approvals recorded here are operator decisions through the same store the
  CLI uses; publication still requires an explicit approval record.
- Prompt text is untrusted content: it is wrapped as data before reaching any
  model (``wrap_untrusted``), never executed as instructions by this server.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, Callable

from freeinference_pr.config import FIConfig
from freeinference_pr.redaction import redact_obj
from freeinference_pr.store import Store, new_id
from freeinference_pr.tools import wrap_untrusted

ACP_PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "freeinference-pr-engineering", "version": "1"}
MAX_LINE = 1_000_000


class ACPServerError(Exception):
    pass


class ACPServer:
    """JSON-RPC loop exposing mission operations over ACP stdio."""

    def __init__(
        self,
        config: FIConfig | None = None,
        *,
        read_line: Callable[[], str | None] | None = None,
        write_line: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config or FIConfig()
        self._read_line = read_line
        self._write_line = write_line
        self._sessions: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ rpc

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Handle one JSON-RPC message. Returns a response dict for requests."""
        if message.get("jsonrpc") != "2.0":
            return self._error(None, -32600, "not a JSON-RPC 2.0 message")
        method = message.get("method", "")
        rid = message.get("id")
        try:
            result = self._dispatch(method, message.get("params") or {})
        except ACPServerError as exc:
            return self._error(rid, -32000, str(exc))
        except Exception as exc:  # noqa: BLE001 - honest protocol failures
            return self._error(rid, -32603, f"{type(exc).__name__}: {exc}")
        if rid is None:
            return None
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                "agentCapabilities": {"loadSession": True},
                "promptCapabilities": {"audio": False, "embeddedContext": False, "image": False},
                "authMethods": [],
                "serverInfo": SERVER_INFO,
            }
        if method == "session/new":
            return self._session_new(params)
        if method == "session/load":
            return self._session_load(params)
        if method == "session/prompt":
            return self._session_prompt(params)
        raise ACPServerError(f"method not supported: {method}")

    @staticmethod
    def _error(rid: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

    # ------------------------------------------------------------- sessions

    def _session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = new_id("acp")
        self._sessions[session_id] = {"cwd": params.get("cwd", ""), "jobs": []}
        return {"sessionId": session_id}

    def _session_load(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = params.get("sessionId", "")
        record = self._sessions.get(session_id)
        if record is None:
            raise ACPServerError(f"unknown session: {session_id}")
        return {"sessionId": session_id, "jobs": record["jobs"]}

    # -------------------------------------------------------------- prompts

    def _session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = params.get("sessionId", "")
        record = self._sessions.get(session_id)
        if record is None:
            raise ACPServerError(f"unknown session: {session_id}")
        blocks = params.get("prompt") or []
        text = "\n".join(
            block.get("text", "") for block in blocks if isinstance(block, dict)
        ).strip()
        if not text:
            raise ACPServerError("empty prompt")
        reply, job_id = self._run_command(text, record)
        if job_id:
            record["jobs"].append(job_id)
        self._notify_update(session_id, reply)
        return {"stopReason": "end_turn"}

    def _run_command(self, text: str, record: dict[str, Any]) -> tuple[str, str | None]:
        store = Store(self.config.state_path)
        try:
            first = text.splitlines()[0].strip()
            lowered = first.lower()
            if lowered == "status" or lowered.startswith("status "):
                return self._status(store, first[6:].strip() or None), None
            parts = first.split(None, 1)
            if len(parts) == 2 and parts[0].lower() in ("approve", "reject"):
                return self._decide(store, parts[0].lower(), parts[1].strip()), None
            if len(parts) == 2 and parts[0].lower() == "cancel":
                return self._cancel(store, parts[1].strip()), None
            return self._submit(store, text, record), None
        finally:
            store.close()

    def _status(self, store: Store, job_id: str | None) -> str:
        if job_id:
            try:
                jobs = [store.get_job(job_id)]
            except KeyError:
                raise ACPServerError(f"unknown job: {job_id}") from None
        else:
            jobs = store.list_jobs()
        lines = []
        for job in jobs:
            lines.append(
                f"{job['id']}  {job['status']}  {job['stage']}  {job['repository']}"
            )
            pending = [a for a in store.approvals_for_job(job["id"]) if a["status"] == "pending"]
            for approval in pending:
                lines.append(f"  pending approval {approval['id']}: {approval['operation']}")
            findings = store.findings_for_job(job["id"])
            open_findings = [f for f in findings if f["status"] != "closed"]
            if open_findings:
                lines.append(f"  open findings: {len(open_findings)}")
        return "\n".join(lines) if lines else "no missions"

    def _decide(self, store: Store, decision: str, approval_id: str) -> str:
        try:
            store.decide_approval(approval_id, decision)
        except Exception as exc:  # noqa: BLE001
            raise ACPServerError(f"approval {approval_id} not recorded: {exc}") from None
        return f"approval {approval_id} -> {decision}"

    def _cancel(self, store: Store, job_id: str) -> str:
        try:
            store.update_job(job_id, status="cancelled")
        except Exception as exc:  # noqa: BLE001
            raise ACPServerError(f"cancel failed for {job_id}: {exc}") from None
        return f"job {job_id} -> cancelled"

    def _submit(self, store: Store, text: str, record: dict[str, Any]) -> str:
        import concurrent.futures

        repository = _parse_repository(text)
        if not repository:
            raise ACPServerError(
                "could not parse a repository (owner/repo) from the request; "
                "include it, or use status/approve/reject/cancel commands"
            )
        self._emit(f"mission accepted for {repository}; planning")

        def _run() -> str:
            # Own store/connection + event loop: this runs in a worker thread
            # when the server loop itself is async, and SQLite connections are
            # thread-bound.
            from freeinference_pr.acp_backend import backend_from_config
            from freeinference_pr.engine import Engine
            from freeinference_pr.tools import ToolRegistry

            own_store = Store(self.config.state_path)
            client = backend_from_config(self.config)
            try:
                job = own_store.create_job("request", repository, text)
                engine = Engine(self.config, own_store, client, ToolRegistry())

                async def _mission() -> str:
                    try:
                        engine.plan(job)
                        return await engine.run_job(job["id"])
                    finally:
                        close = getattr(client, "close", None)
                        if close:
                            await close()

                status = asyncio.run(_mission())
                return f"mission {job['id']} -> {status}"
            except Exception as exc:  # noqa: BLE001 - report honestly, never fake success
                return f"mission failed: {type(exc).__name__}: {exc}"
            finally:
                own_store.close()

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return _run()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_run).result()

    # ------------------------------------------------------------ streaming

    def _notify_update(self, session_id: str, text: str) -> None:
        self._send({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {"sessionUpdate": "agent_message_chunk",
                           "content": {"type": "text", "text": text}},
            },
        })

    def _emit(self, text: str) -> None:
        """Progress line for the CURRENT prompt (called during _submit)."""
        self._send({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": self._current_session or "",
                       "update": {"sessionUpdate": "agent_message_chunk",
                                  "content": {"type": "text", "text": text}}},
        })

    _current_session: str | None = None

    # ------------------------------------------------------------------ io

    def _send(self, message: dict[str, Any]) -> None:
        if self._write_line is None:
            sys.stdout.write(json.dumps(redact_obj(message), default=str) + "\n")
            sys.stdout.flush()
        else:
            self._write_line(json.dumps(redact_obj(message), default=str))

    def serve(self) -> int:
        """Blocking stdio loop. Returns a process exit code."""
        source = self._read_line if self._read_line else sys.stdin.readline
        while True:
            line = source()
            if line is None:
                return 0
            line = line.strip()
            if not line:
                continue
            if len(line) > MAX_LINE:
                self._send(self._error(None, -32600, "message exceeds the 1 MB framing bound"))
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                self._send(self._error(None, -32700, f"parse error: {exc}"))
                continue
            if isinstance(message.get("method"), str) and message.get("method") == "session/prompt":
                self._current_session = (message.get("params") or {}).get("sessionId")
            response = self.handle_message(message)
            if response is not None:
                self._send(response)
            if isinstance(message.get("method"), str):
                self._current_session = None


def _parse_repository(text: str) -> str:
    """Extract an owner/repo slug from free text (untrusted content)."""
    import re

    match = re.search(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b", text)
    if not match:
        return ""
    candidate = match.group(1).strip(".,;:)")
    owner, _, repo = candidate.partition("/")
    if not repo or "." in owner or owner.lower() in ("http", "https", "www"):
        return ""
    return candidate


def wrap_prompt(text: str) -> str:
    return wrap_untrusted(text, "acp_prompt")
