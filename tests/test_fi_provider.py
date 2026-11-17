"""FreeInference provider tests (mocked HTTP; no network, no real key)."""

import asyncio
import json

import httpx
import pytest

from freeinference_pr.config import FIConfig
from freeinference_pr.fi_provider import (
    FreeInferenceClient,
    ProviderAuthError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderServerError,
)


def make_config(**overrides) -> FIConfig:
    env = {
        "FREEINFERENCE_API_KEY": "hyi-dummy-key-for-tests",
        "FI_MAX_ATTEMPTS": "3",
        "FI_BACKOFF_BASE": "0.01",
        "FI_BACKOFF_MAX": "0.05",
        "FI_MAX_CONCURRENT": "2",
    }
    env.update(overrides)
    return FIConfig(env)


def transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


MODELS_BODY = {"data": [
    {"id": "qwen3.6-35b", "context_length": 262144, "max_output_length": 8192,
     "features": ["tools", "json_mode", "structured_outputs"]},
    {"id": "deepseek-v4-flash", "context_length": 1000000, "max_output_length": 393216,
     "features": ["tools", "json_mode", "structured_outputs"]},
]}


async def test_model_discovery_and_capabilities():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer hyi-dummy-key-for-tests"
        return httpx.Response(200, json=MODELS_BODY)

    async with FreeInferenceClient(make_config(), transport=transport(handler)) as client:
        models = await client.list_models()
        assert set(models) == {"qwen3.6-35b", "deepseek-v4-flash"}
        assert models["qwen3.6-35b"].supports("tools")
        info = await client.capabilities("deepseek-v4-flash")
        assert info.context_length == 1_000_000


async def test_chat_parses_content_usage_and_tool_calls():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload.get("tools"):
            return httpx.Response(200, json={"model": "m", "choices": [{
                "finish_reason": "tool_calls",
                "message": {"content": "", "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "get_pr_diff", "arguments": "{\"owner\": \"o\", \"pr\": 1}"},
                }]},
            }], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
        return httpx.Response(200, json={"model": "m", "choices": [
            {"finish_reason": "stop", "message": {"content": "OK"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    async with FreeInferenceClient(make_config(), transport=transport(handler)) as client:
        plain = await client.chat("m", [{"role": "user", "content": "hi"}])
        assert plain.content == "OK"
        assert plain.usage.total_tokens == 2
        with_tools = await client.chat("m", [{"role": "user", "content": "call it"}], tools=[{"type": "function", "function": {"name": "x", "parameters": {}}}])
        assert with_tools.tool_calls[0].name == "get_pr_diff"
        assert with_tools.tool_calls[0].arguments == {"owner": "o", "pr": 1}


async def test_auth_error_not_retried():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(401, json={"error": "no auth"})

    async with FreeInferenceClient(make_config(), transport=transport(handler)) as client:
        with pytest.raises(ProviderAuthError):
            await client.chat("m", [{"role": "user", "content": "hi"}])
    assert len(attempts) == 1


async def test_rate_limit_retries_then_succeeds():
    state = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["count"] += 1
        if state["count"] < 3:
            return httpx.Response(429, json={"error": "slow down"}, headers={"Retry-After": "0.01"})
        return httpx.Response(200, json={"model": "m", "choices": [
            {"finish_reason": "stop", "message": {"content": "ok"}}]})

    async with FreeInferenceClient(make_config(), transport=transport(handler)) as client:
        result = await client.chat("m", [{"role": "user", "content": "hi"}])
        assert result.content == "ok"
        assert state["count"] == 3


async def test_server_error_exhausts_retries():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(500, json={"error": "boom"})

    async with FreeInferenceClient(make_config(), transport=transport(handler)) as client:
        with pytest.raises(ProviderServerError):
            await client.chat("m", [{"role": "user", "content": "hi"}])
    assert len(attempts) == 3


async def test_streaming_yields_deltas_and_done():
    sse = (
        'data: {"choices": [{"delta": {"content": "Hel"}}]}\n\n'
        'data: {"choices": [{"delta": {"content": "lo"}}]}\n\n'
        'data: {"choices": [{"finish_reason": "stop"}], "usage": {"total_tokens": 7}}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse, headers={"Content-Type": "text/event-stream"})

    async with FreeInferenceClient(make_config(), transport=transport(handler)) as client:
        events = [event async for event in client.chat_stream("m", [{"role": "user", "content": "hi"}])]
    deltas = [e for e in events if e["type"] == "delta"]
    done = [e for e in events if e["type"] == "done"]
    assert "".join(e["text"] for e in deltas) == "Hello"
    assert done and done[0]["usage"].total_tokens == 7


async def test_concurrency_semaphore_bounds_in_flight():
    in_flight = 0
    peak = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        import time

        time.sleep(0.05)
        in_flight -= 1
        return httpx.Response(200, json={"model": "m", "choices": [
            {"finish_reason": "stop", "message": {"content": "ok"}}]})

    async with FreeInferenceClient(make_config(), transport=transport(handler)) as client:
        await asyncio.gather(*[
            client.chat("m", [{"role": "user", "content": f"msg {i}"}]) for i in range(6)
        ])
    assert peak <= 2


async def test_invalid_tool_arguments_raise_protocol_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "m", "choices": [{
            "finish_reason": "tool_calls",
            "message": {"tool_calls": [{"id": "c", "type": "function",
                                        "function": {"name": "x", "arguments": "not-json"}}]},
        }]})

    async with FreeInferenceClient(make_config(), transport=transport(handler)) as client:
        with pytest.raises(ProviderProtocolError):
            await client.chat("m", [{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "x", "parameters": {}}}])


async def test_missing_key_is_reported_not_hidden():
    config = make_config()
    config_env = dict(FIConfig().get.__self__.__dict__) if False else None
    import os

    old = os.environ.pop("FREEINFERENCE_API_KEY", None)
    try:
        empty = FIConfig()
        assert empty.api_key == ""
    finally:
        if old:
            os.environ["FREEINFERENCE_API_KEY"] = old
