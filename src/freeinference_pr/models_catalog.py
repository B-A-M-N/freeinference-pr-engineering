"""Role-to-model assignment over the verified FreeInference catalog.

Assignments prioritize demonstrated tool-calling and structured-output support
(both chat models declare ``tools`` and ``structured_outputs``) over reputation.
Investigation and implementation get the 1M-context model; independent review
deliberately uses a *different* model than implementation so the review is not
a self-check by the same weights.
"""

from __future__ import annotations

from pydantic import BaseModel

from freeinference_pr.provider import FreeInferenceCompletionConfig

ROLE = {
    "orchestrator": "orchestrator",
    "investigation": "investigation",
    "implementation": "implementation",
    "review": "review",
    "verification": "verification",
    "packaging": "packaging",
}


class ModelPreset(BaseModel):
    model: str
    context_window: int
    max_output_tokens: int


# deepseek-v4-flash: 1M context for large diffs and code reading.
# qwen3.6-35b: high-speed, structured outputs, vision-capable.
ROLE_PRESETS: dict[str, ModelPreset] = {
    "orchestrator": ModelPreset(model="qwen3.6-35b", context_window=262_144, max_output_tokens=8_192),
    "investigation": ModelPreset(model="deepseek-v4-flash", context_window=1_000_000, max_output_tokens=32_768),
    "implementation": ModelPreset(model="deepseek-v4-flash", context_window=1_000_000, max_output_tokens=32_768),
    "review": ModelPreset(model="qwen3.6-35b", context_window=262_144, max_output_tokens=8_192),
    "verification": ModelPreset(model="deepseek-v4-flash", context_window=1_000_000, max_output_tokens=32_768),
    "packaging": ModelPreset(model="qwen3.6-35b", context_window=262_144, max_output_tokens=8_192),
}


def completion_for_role(role: str) -> FreeInferenceCompletionConfig:
    preset = ROLE_PRESETS[role]
    return FreeInferenceCompletionConfig(
        model=preset.model,
        context_window=preset.context_window,
        max_output_tokens=preset.max_output_tokens,
    )
