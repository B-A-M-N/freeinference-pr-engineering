"""ACP integration tests: agent backend, server mode, UDS broker transport.

No network: the ACP agent is a minimal in-repo subprocess speaking the
newline-delimited JSON-RPC contract verified in Kontrol's source; the UDS
broker is a local asyncio Unix-socket HTTP stub.
"""

import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

from freeinference_pr.acp_backend import ACPAgentBackend, backend_from_config
from freeinference_pr.acp_server import ACPServer, _parse_repository
from freeinference_pr.config import FIConfig
from freeinference_pr.fi_provider import FreeInferenceClient

FAKE_ACP_AGENT = textwrap.dedent("""
    import json, sys
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method", "")
        rid = msg.get("id")
        if method == "initialize":
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2025-06-18", "agentCapabilities": {}}}), flush=True)
        elif method == "session/new":
            print(json.dumps({"jsonrpc": "2.0", "id": rid,
                              "result": {"sessionId": "sess-fake-1"}}), flush=True)
        elif method == "session/prompt":
            text = "".join(b.get("text", "") for b in msg["params"]["prompt"]
                           if isinstance(b, dict))
            update = {"jsonrpc": "2.0", "method": "session/update", "params": {
                "sessionId": "sess-fake-1",
                "update": {"sessionUpdate": "agent_message_chunk",
                           "content": {"type": "text", "text": "ACP-REPLY:" + text[:20]}}}}
            print(json.dumps(update), flush=True)
            print(json.dumps({"jsonrpc": "2.0", "id": rid,
                              "result": {"stopReason": "end_turn"}}), flush=True)
        elif rid is not None:
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32601, "message": "unsupported"}}), flush=True)
""")


@pytest.fixture()
def fake_acp_command(tmp_path):
    script = tmp_path / "fake_acp_agent.py"
    script.write_text(FAKE_ACP_AGENT)
    return f"{sys.executable} {script}"


def make_config(tmp_path, **extra):
    env = {
        "FI_STATE_PATH": str(tmp_path / "state.db"),
        "FI_WORKSPACE_ROOT": str(tmp_path / "ws"),
        "FI_MAX_AGENT_TURNS": "3",
        "FI_BACKOFF_BASE": "0.01",
        "FI_BACKOFF_MAX": "0.02",
    }
    env.update(extra)
    return FIConfig(env)


# ----------------------------------------------------------------- backend


async def test_acp_backend_drives_agent_lifecycle(tmp_path, fake_acp_command):
    config = make_config(tmp_path, FI_ACP_COMMAND=fake_acp_command)
    backend = ACPAgentBackend(config)
    try:
        result = await backend.chat("any-model", [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hello agent"},
        ])
        assert result.content.startswith("ACP-REPLY:")
        assert result.finish_reason == "end_turn"
        assert result.tool_calls == []
        assert result.usage.total_tokens == 0
    finally:
        await backend.close()


async def test_acp_backend_requires_command(tmp_path):
    config = make_config(tmp_path)
    backend = ACPAgentBackend(config)
    with pytest.raises(Exception, match="FI_ACP_COMMAND"):
        await backend.chat("m", [{"role": "user", "content": "x"}])
    await backend.close()


async def test_backend_from_config_selects_acp(tmp_path, fake_acp_command):
    config = make_config(tmp_path, FI_ACP_COMMAND=fake_acp_command)
    backend = backend_from_config(config)
    assert isinstance(backend, ACPAgentBackend)
    await backend.close()


async def test_backend_from_config_defaults_to_http(tmp_path):
    config = make_config(tmp_path)
    backend = backend_from_config(config)
    assert isinstance(backend, FreeInferenceClient)
    await backend.close()


async def test_acp_backend_fails_closed_on_agent_requests(tmp_path):
    """An ACP agent asking us for fs/write must not get it served; the chat
    still completes."""
    agent = textwrap.dedent("""
        import json, sys
        for line in sys.stdin:
            msg = json.loads(line.strip())
            if msg.get("method") == "initialize":
                print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {}}), flush=True)
            elif msg.get("method") == "session/new":
                print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                  "result": {"sessionId": "s"}}), flush=True)
            elif msg.get("method") == "session/prompt":
                print(json.dumps({"jsonrpc": "2.0", "id": 999, "method": "fs/write_text_file",
                                  "params": {"path": "/tmp/fi-evil-probe", "content": "x"}}), flush=True)
                print(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                  "result": {"stopReason": "end_turn"}}), flush=True)
    """)
    script = tmp_path / "pushy_agent.py"
    script.write_text(agent)
    config = make_config(tmp_path, FI_ACP_COMMAND=f"{sys.executable} {script}")
    backend = ACPAgentBackend(config)
    try:
        result = await backend.chat("m", [{"role": "user", "content": "hi"}])
        assert result.finish_reason == "end_turn"
        assert not Path("/tmp/fi-evil-probe").exists()
    finally:
        await backend.close()


# ------------------------------------------------------------------ server


def rpc(msg):
    return json.dumps(msg)


def drive(server, messages):
    out = []
    server._write_line = lambda raw: out.append(json.loads(raw))
    responses = []
    for raw in messages:
        response = server.handle_message(json.loads(raw))
        if response is not None:
            responses.append(response)
    return responses, out


def make_server(tmp_path):
    return ACPServer(make_config(tmp_path))


def test_acp_server_initialize_and_sessions(tmp_path):
    server = make_server(tmp_path)
    (responses, _) = drive(server, [
        rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {}}}),
        rpc({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": "/tmp"}}),
    ])
    assert responses[0]["result"]["protocolVersion"] == "2025-06-18"
    assert responses[0]["result"]["serverInfo"]["name"] == "freeinference-pr-engineering"
    assert responses[1]["result"]["sessionId"].startswith("acp_")


def test_acp_server_status_prompt_streams_update(tmp_path):
    server = make_server(tmp_path)
    (responses, _) = drive(server, [
        rpc({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {}}),
    ])
    session_id = responses[0]["result"]["sessionId"]
    (responses, notifications) = drive(server, [
        rpc({"jsonrpc": "2.0", "id": 2, "method": "session/prompt",
             "params": {"sessionId": session_id,
                        "prompt": [{"type": "text", "text": "status"}]}}),
    ])
    assert responses[0]["result"]["stopReason"] == "end_turn"
    updates = [n for n in notifications if n.get("method") == "session/update"]
    assert updates, "status reply must stream as session/update"
    text = updates[-1]["params"]["update"]["content"]["text"]
    assert "no missions" in text


def test_acp_server_submit_reports_failure_honestly(tmp_path, monkeypatch):
    config = make_config(tmp_path)

    class FailingBackend:
        async def chat(self, *a, **k):
            raise RuntimeError("backend down")

        async def close(self):
            return None

    monkeypatch.setattr("freeinference_pr.acp_backend.backend_from_config",
                        lambda cfg: FailingBackend())
    server = ACPServer(config)
    (responses, _) = drive(server, [
        rpc({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {}}),
    ])
    session_id = responses[0]["result"]["sessionId"]
    (responses, notifications) = drive(server, [
        rpc({"jsonrpc": "2.0", "id": 2, "method": "session/prompt",
             "params": {"sessionId": session_id,
                        "prompt": [{"type": "text",
                                    "text": "please fix the bug in octo/example"}]}}),
    ])
    text = notifications[-1]["params"]["update"]["content"]["text"]
    assert "-> failed" in text, f"backend failure must surface as a failed mission: {text}"
    assert "mission" in text


def test_acp_server_unknown_session_and_method_fail_closed(tmp_path):
    server = make_server(tmp_path)
    (responses, _) = drive(server, [
        rpc({"jsonrpc": "2.0", "id": 1, "method": "session/prompt",
             "params": {"sessionId": "nope", "prompt": [{"type": "text", "text": "hi"}]}}),
        rpc({"jsonrpc": "2.0", "id": 2, "method": "fs/write_text_file",
             "params": {"path": "/tmp/x"}}),
    ])
    assert "error" in responses[0]
    assert "not supported" in responses[1]["error"]["message"]


def test_acp_server_serve_loop_io(tmp_path):
    server = make_server(tmp_path)
    feed = iter([
        rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        "",
        "not json",
        rpc({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {}}),
    ])
    sent = []
    server._read_line = lambda: next(feed, None)
    server._write_line = sent.append
    code = server.serve()
    assert code == 0
    kinds = ["error" if "error" in json.loads(raw) else "result" for raw in sent]
    assert kinds == ["result", "error", "result"]


def test_parse_repository_slugs():
    assert _parse_repository("fix bug in octo/example please") == "octo/example"
    assert _parse_repository("https://github.com/a/b") == ""
    assert _parse_repository("no repo here") == ""


# ----------------------------------------------------- UDS broker transport


async def test_uds_broker_transport(tmp_path):
    """FI_INFERENCE_SOCKET routes inference over a Unix-socket HTTP transport."""
    received = {}

    async def handle(reader, writer):
        data = await reader.read(65536)
        received["auth"] = "Authorization" in data.decode("latin1", "replace")
        body = json.dumps({
            "choices": [{"message": {"content": "uds-ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                     + str(len(body)).encode() + b"\r\n\r\n" + body)
        await writer.drain()
        writer.close()

    socket_path = tmp_path / "fi.sock"
    server = await asyncio.start_unix_server(handle, path=str(socket_path))
    async with server:
        config = make_config(tmp_path, FI_INFERENCE_SOCKET=str(socket_path),
                             FREEINFERENCE_API_KEY="")
        client = FreeInferenceClient(config)
        try:
            result = await client.chat("m", [{"role": "user", "content": "hi"}])
            assert result.content == "uds-ok"
        finally:
            await client.close()
    assert received["auth"] is False, "no API key may travel to the broker"


def test_mistral_key_env_preset(tmp_path, monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "test-mistral-key")
    config = make_config(tmp_path, FI_API_KEY_ENV="MISTRAL_API_KEY",
                         FREEINFERENCE_BASE_URL="https://api.mistral.ai/v1")
    assert config.api_key == "test-mistral-key"
    assert config.base_url == "https://api.mistral.ai/v1"
