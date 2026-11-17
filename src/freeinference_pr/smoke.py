"""Real FreeInference integration smoke test (requires credentials + network).

Exercises: model discovery, chat, tool calling, streaming, multi-turn recall.
Prints one PASS/FAIL line per check and exits non-zero on any failure. Mocked
tests elsewhere in the suite do NOT substitute for this check.
"""

from __future__ import annotations

import asyncio
import sys

from freeinference_pr.config import FIConfig
from freeinference_pr.fi_provider import FreeInferenceClient, ProviderError


async def run_smoke(config: FIConfig) -> int:
    if not config.api_key:
        print("smoke: FREEINFERENCE_API_KEY not set; real integration NOT verified", file=sys.stderr)
        return 2
    results: list[tuple[str, bool, str]] = []
    async with FreeInferenceClient(config) as client:
        try:
            models = await client.list_models()
            results.append(("model discovery", bool(models), f"{len(models)} models"))
            model = config.model_for("orchestrator")
            if model not in models:
                model = next(iter(models), "")
            chat = await client.chat(model, [{"role": "user", "content": "Reply with exactly: OK"}], max_tokens=10)
            results.append(("chat completion", chat.content.strip() != "", chat.content[:40]))
            tool_chat = await client.chat(
                model,
                [{"role": "user", "content": "Call get_pr_diff for owner 'o', repo 'r', pr 1."}],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "get_pr_diff",
                        "description": "Fetch a PR diff",
                        "parameters": {
                            "type": "object",
                            "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}, "pr": {"type": "integer"}},
                            "required": ["owner", "repo", "pr"],
                        },
                    },
                }],
                max_tokens=200,
            )
            results.append(("tool calling", bool(tool_chat.tool_calls),
                            tool_chat.tool_calls[0].name if tool_chat.tool_calls else "no tool call"))
            streamed = ""
            usage_total = 0
            async for event in client.chat_stream(model, [{"role": "user", "content": "Count from 1 to 5."}], max_tokens=60):
                if event["type"] == "delta":
                    streamed += event["text"]
                elif event["type"] == "done":
                    usage_total = event["usage"].total_tokens
            results.append(("streaming", len(streamed) > 0, f"{len(streamed)} chars, usage={usage_total}"))
            multi = await client.chat(
                model,
                [
                    {"role": "user", "content": "My mission code is MISSION-42."},
                    {"role": "assistant", "content": "Noted, your mission code is MISSION-42."},
                    {"role": "user", "content": "What is my mission code? Reply with only the code."},
                ],
                max_tokens=20,
            )
            results.append(("multi-turn recall", "MISSION-42" in multi.content, multi.content[:40]))
        except ProviderError as exc:
            results.append(("provider error", False, str(exc)))

    ok = True
    for name, passed, note in results:
        ok = ok and passed
        print(f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" — {note}" if note else ""))
    print("REAL INTEGRATION VERIFIED" if ok else "REAL INTEGRATION FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run_smoke(FIConfig())))
