"""ACP agent backend — use any Agent Client Protocol agent as an inference source.

The Agent Client Protocol (ACP) is newline-delimited JSON-RPC 2.0 over stdio
(verified against Kontrol's ``src/acp-duplex.ts`` and
``scripts/qualify-acp-harness.mjs``):

    initialize   {protocolVersion, capabilities, clientInfo} -> {protocolVersion, agentCapabilities, ...}
    session/new  {cwd, mcpServers}                           -> {sessionId}
    session/prompt {sessionId, prompt: [{type: "text", text}]} -> {stopReason}
    session/update (notification) {sessionId, update: {sessionUpdate: "agent_message_chunk", ...}}

This backend satisfies the same client contract as ``FreeInferenceClient``
(``chat(model, messages, tools, temperature) -> ChatResult`` + ``close()``), so
the agent runtime, engine, and Runner bridge work unchanged against any ACP
agent harness (Kontrol, Zed-compatible agents, custom duplex adapters).

Honest capability boundary: ACP agents run their own tools; this backend does
not translate our tool schemas into ACP. ``chat()`` therefore returns text-only
results (``tool_calls`` empty) and reports usage as unknown — the runtime's
text-parse path handles structured outputs, and callers that require native
tool calling should keep an OpenAI-compatible backend.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from typing import Any

from freeinference_pr.config import FIConfig
from freeinference_pr.fi_provider import ChatResult, ToolCall, Usage

ACP_PROTOCOL_VERSION = "2025-06-18"
INIT_TIMEOUT = 30.0
PROMPT_TIMEOUT = 600.0


class ACPBackendError(Exception):
    pass


class _ACPSession:
    """One spawned ACP agent subprocess with a JSON-RPC stdio duplex."""

    def __init__(self, command: list[str], cwd: str = "") -> None:
        self.command = command
        self.cwd = cwd
        self.proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._updates: list[dict[str, Any]] = []
        self._reader_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self.cwd or None,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
        except (OSError, FileNotFoundError) as exc:
            raise ACPBackendError(f"cannot spawn ACP agent {self.command}: {exc}") from None
        self._reader_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._dispatch(message)
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ACPBackendError("ACP agent stream closed"))

    def _dispatch(self, message: dict[str, Any]) -> None:
        if isinstance(message.get("id"), int) and ("result" in message or "error" in message):
            future = self._pending.pop(message["id"], None)
            if future and not future.done():
                if "error" in message:
                    err = message["error"]
                    future.set_exception(
                        ACPBackendError(f"ACP error {err.get('code')}: {err.get('message')}")
                    )
                else:
                    future.set_result(message.get("result"))
            return
        method = message.get("method", "")
        if method == "session/update":
            update = (message.get("params") or {}).get("update") or {}
            self._updates.append(update)
            return
        # Agent-initiated requests we do not serve: fail closed.
        if isinstance(message.get("id"), int) and self.proc and self.proc.stdin:
            response = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": f"method not supported: {method}"},
            }
            self.proc.stdin.write((json.dumps(response) + "\n").encode())

    async def request(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        if not self.proc or not self.proc.stdin:
            raise ACPBackendError("ACP agent not started")
        self._next_id += 1
        rid = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        message = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        await self.proc.stdin.drain()
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise ACPBackendError(f"ACP {method} timed out after {timeout}s") from None

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()
        if self._reader_task:
            self._reader_task.cancel()


class ACPAgentBackend:
    """Inference backend backed by any ACP agent harness.

    Config (env):
        FI_ACP_COMMAND   shell command string of the ACP agent (required)
        FI_ACP_CWD       working directory for the agent session (default: cwd)
    """

    def __init__(self, config: FIConfig, session: _ACPSession | None = None) -> None:
        self.config = config
        self._session = session
        self._owns_session = session is None
        self._session_id: str | None = None

    async def _ensure_session(self) -> _ACPSession:
        if self._session is not None:
            return self._session
        command_str = self.config.get("FI_ACP_COMMAND", "")
        if not command_str:
            raise ACPBackendError("FI_ACP_COMMAND is required for the ACP backend")
        session = _ACPSession(
            shlex.split(command_str), cwd=self.config.get("FI_ACP_CWD", os.getcwd())
        )
        await session.start()
        result = await session.request(
            "initialize",
            {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "freeinference-pr-engineering", "version": "1"},
            },
            timeout=INIT_TIMEOUT,
        )
        if not isinstance(result, dict):
            raise ACPBackendError("ACP initialize returned no result")
        created = await session.request("session/new", {"cwd": session.cwd, "mcpServers": []},
                                        timeout=INIT_TIMEOUT)
        self._session_id = (created or {}).get("sessionId")
        if not self._session_id:
            raise ACPBackendError(f"ACP session/new returned no sessionId: {created}")
        self._session = session
        return session

    async def list_models(self) -> dict[str, Any]:
        return {}

    async def chat(
        self, model: str, messages: list[dict[str, Any]], tools: Any = None,
        temperature: float = 0.2, **kwargs: Any,
    ) -> ChatResult:
        session = await self._ensure_session()
        prompt_text = _render_messages(messages)
        session._updates.clear()
        result = await session.request(
            "session/prompt",
            {"sessionId": self._session_id, "prompt": [{"type": "text", "text": prompt_text}]},
            timeout=PROMPT_TIMEOUT,
        )
        chunks = [
            u.get("content", {}).get("text", "")
            for u in session._updates
            if isinstance(u, dict) and u.get("sessionUpdate") == "agent_message_chunk"
        ]
        content = "".join(chunks)
        if not content and isinstance(result, dict):
            content = str(result.get("text", ""))
        return ChatResult(
            content=content,
            tool_calls=[],
            usage=Usage(0, 0, 0),
            finish_reason=str((result or {}).get("stopReason", "end_turn")),
            model=model,
        )

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None


def _render_messages(messages: list[dict[str, Any]]) -> str:
    """Flatten a chat-messages list into one prompt text block."""
    parts: list[str] = []
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            )
        parts.append(f"[{role}]\n{content or ''}")
    return "\n\n".join(parts)


def backend_from_config(config: FIConfig):
    """Select the inference backend from configuration.

    FI_ACP_COMMAND set  -> ACP agent backend (any ACP harness).
    otherwise           -> OpenAI-compatible HTTP client (FreeInference,
                           Mistral via FI_API_KEY_ENV/FREEINFERENCE_BASE_URL,
                           or a task-scoped UDS broker via FI_INFERENCE_SOCKET).
    """
    from freeinference_pr.fi_provider import FreeInferenceClient

    if config.get("FI_ACP_COMMAND", ""):
        return ACPAgentBackend(config)
    return FreeInferenceClient(config)
