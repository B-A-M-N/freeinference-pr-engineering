"""FreeInference client: OpenAI-compatible chat, streaming, tools, retries.

Independent of Mistral: plain HTTP against ``FREEINFERENCE_BASE_URL`` with
``FREEINFERENCE_API_KEY``. Bounded exponential backoff, per-request timeouts,
cancellation, concurrency limiting, capability detection, usage reporting, and
provider error classification.
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from freeinference_pr.config import FIConfig


class ProviderError(Exception):
    """Base class for classified provider failures."""

    retryable = False

    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body[:2000]


class ProviderAuthError(ProviderError):
    retryable = False


class ProviderModelNotFoundError(ProviderError):
    retryable = False


class ProviderRateLimitError(ProviderError):
    retryable = True

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class ProviderServerError(ProviderError):
    retryable = True


class ProviderNetworkError(ProviderError):
    retryable = True


class ProviderProtocolError(ProviderError):
    retryable = False


class ProviderCancelled(ProviderError):
    retryable = False


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class ChatResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelInfo:
    id: str
    context_length: int = 0
    max_output_tokens: int = 0
    features: list[str] = field(default_factory=list)

    def supports(self, feature: str) -> bool:
        return feature in self.features


def classify_status(status: int, body: str, retry_after: float | None = None) -> ProviderError:
    if status in (401, 403):
        return ProviderAuthError(f"authentication failed ({status})", status=status, body=body)
    if status == 404:
        return ProviderModelNotFoundError("model or endpoint not found (404)", status=status, body=body)
    if status == 429:
        return ProviderRateLimitError("rate limited (429)", retry_after=retry_after, status=status, body=body)
    if 500 <= status < 600:
        return ProviderServerError(f"provider server error ({status})", status=status, body=body)
    return ProviderError(f"provider error ({status})", status=status, body=body)


class FreeInferenceClient:
    """Async FreeInference client. All requests pass through one semaphore."""

    def __init__(self, config: FIConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self._semaphore = asyncio.Semaphore(config.max_concurrent)
        headers = {"Content-Type": "application/json", "User-Agent": "freeinference-pr-engineering"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        self._client = httpx.AsyncClient(
            base_url=config.base_url, headers=headers, timeout=config.request_timeout,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "FreeInferenceClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------ models

    async def list_models(self) -> dict[str, ModelInfo]:
        data = await self._request_json("GET", "/models")
        models: dict[str, ModelInfo] = {}
        for entry in data.get("data", []):
            info = ModelInfo(
                id=entry.get("id", ""),
                context_length=int(entry.get("context_length") or 0),
                max_output_tokens=int(entry.get("max_output_length") or 0),
                features=list(entry.get("features") or []),
            )
            if info.id:
                models[info.id] = info
        return models

    async def capabilities(self, model: str) -> ModelInfo:
        models = await self.list_models()
        if model not in models:
            raise ProviderModelNotFoundError(
                f"model {model!r} not in provider catalog: {sorted(models)}"
            )
        return models[model]

    # -------------------------------------------------------------- chat

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.2,
    ) -> ChatResult:
        payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if max_tokens:
            payload["max_tokens"] = max_tokens

        last_error: ProviderError | None = None
        for attempt in range(self.config.max_attempts):
            try:
                async with self._semaphore:
                    response = await self._client.post("/chat/completions", json=payload)
            except asyncio.CancelledError:
                raise
            except httpx.TimeoutException as exc:
                last_error = ProviderNetworkError(f"request timed out: {exc}")
            except httpx.HTTPError as exc:
                last_error = ProviderNetworkError(f"network error: {exc}")
            else:
                retry_after = _retry_after_seconds(response.headers)
                if response.status_code == 200:
                    return _parse_chat_response(response, model)
                last_error = classify_status(response.status_code, response.text, retry_after)
            if not last_error.retryable or attempt == self.config.max_attempts - 1:
                raise last_error
            await asyncio.sleep(_backoff(self.config, attempt, last_error))
        raise last_error  # pragma: no cover - loop always returns or raises

    async def chat_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield {"type": "delta", "text": ...} events, then a final {"type": "done", ...}."""
        payload: dict[str, Any] = {
            "model": model, "messages": messages, "stream": True, "temperature": 0.2,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        last_error: ProviderError | None = None
        for attempt in range(self.config.max_attempts):
            try:
                async with self._semaphore:
                    async with self._client.stream("POST", "/chat/completions", json=payload) as response:
                        if response.status_code != 200:
                            body = (await response.aread()).decode("utf-8", "replace")
                            last_error = classify_status(response.status_code, body)
                            if not last_error.retryable:
                                raise last_error
                            await asyncio.sleep(_backoff(self.config, attempt, last_error))
                            continue
                        async for event in _iter_sse_events(response):
                            yield event
                        return
            except asyncio.CancelledError:
                raise
            except httpx.HTTPError as exc:
                last_error = ProviderNetworkError(f"network error during stream: {exc}")
                if attempt == self.config.max_attempts - 1:
                    raise last_error
                await asyncio.sleep(_backoff(self.config, attempt, last_error))
        if last_error is not None:
            raise last_error

    # ------------------------------------------------------------- internals

    async def _request_json(self, method: str, path: str) -> dict[str, Any]:
        async with self._semaphore:
            try:
                response = await self._client.request(method, path)
            except asyncio.CancelledError:
                raise
            except httpx.HTTPError as exc:
                raise ProviderNetworkError(f"network error: {exc}") from exc
        if response.status_code != 200:
            raise classify_status(response.status_code, response.text)
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderProtocolError(f"invalid JSON from provider: {exc}") from exc


def _retry_after_seconds(headers: httpx.Headers) -> float | None:
    value = headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _backoff(config: FIConfig, attempt: int, error: ProviderError) -> float:
    if isinstance(error, ProviderRateLimitError) and error.retry_after:
        return min(error.retry_after, config.backoff_max_seconds)
    delay = config.backoff_base_seconds * (2**attempt)
    return min(delay, config.backoff_max_seconds) * (0.5 + random.random() * 0.5)


def _parse_chat_response(response: httpx.Response, model: str) -> ChatResult:
    try:
        body = response.json()
    except ValueError as exc:
        raise ProviderProtocolError(f"invalid JSON in chat response: {exc}") from exc
    choices = body.get("choices") or []
    if not choices:
        raise ProviderProtocolError("chat response has no choices")
    message = choices[0].get("message") or {}
    tool_calls: list[ToolCall] = []
    for index, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function") or {}
        raw_arguments = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else dict(raw_arguments)
        except ValueError as exc:
            raise ProviderProtocolError(
                f"tool call {index} has non-JSON arguments: {exc}"
            ) from exc
        tool_calls.append(
            ToolCall(
                call_id=str(call.get("id") or f"call_{index}"),
                name=str(function.get("name") or ""),
                arguments=arguments,
            )
        )
    usage_raw = body.get("usage") or {}
    usage = Usage(
        prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
        completion_tokens=int(usage_raw.get("completion_tokens") or 0),
        total_tokens=int(usage_raw.get("total_tokens") or 0),
    )
    return ChatResult(
        content=str(message.get("content") or ""),
        tool_calls=tool_calls,
        usage=usage,
        finish_reason=str(choices[0].get("finish_reason") or ""),
        model=str(body.get("model") or model),
        raw=body,
    )


async def _iter_sse_events(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    usage: Usage | None = None
    finish_reason = ""
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except ValueError:
            continue
        choices = chunk.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            text = delta.get("content")
            if text:
                yield {"type": "delta", "text": str(text)}
            if choices[0].get("finish_reason"):
                finish_reason = str(choices[0]["finish_reason"])
        usage_raw = chunk.get("usage")
        if usage_raw:
            usage = Usage(
                prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
                completion_tokens=int(usage_raw.get("completion_tokens") or 0),
                total_tokens=int(usage_raw.get("total_tokens") or 0),
            )
    yield {
        "type": "done",
        "finish_reason": finish_reason,
        "usage": usage or Usage(),
    }
