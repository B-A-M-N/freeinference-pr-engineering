"""FreeInference completion provider for the Mistral Agents SDK.

FreeInference (https://freeinference.org/v1) speaks the OpenAI Chat Completions
protocol, so this config reuses the SDK's ``OpenAICompletionProvider`` and only
carries the gateway settings. The config is serialized and sent to deployed
workers, therefore it stores the *name* of the environment variable holding the
API key, never the key itself. The key is read in the process that builds the
provider (``build_provider``), which on a managed deployment is the worker whose
environment has the secret bound at deploy time.
"""

from __future__ import annotations

import os

from typing import Literal

from mistralai.vibe.harness.runtime.completion import (
    CompletionConfigBase,
    CompletionProvider,
    RetryConfig,
)
from mistralai.vibe.harness.runtime.completion.providers.openai import (
    OpenAICompletionProvider,
)

FREEINFERENCE_BASE_URL = "https://freeinference.org/v1"
FREEINFERENCE_API_KEY_ENV_VAR = "FREEINFERENCE_API_KEY"

# Verified against GET /v1/models on 2026-11-16 (3 models; see tests and docs/GATES.md).
VERIFIED_MODEL_CATALOG: dict[str, dict[str, object]] = {
    "qwen3.6-35b": {
        "context_length": 262_144,
        "max_output_length": 8_192,
        "features": ["tools", "json_mode", "structured_outputs"],
        "input_modalities": ["text", "image", "video"],
        "owned_by": "vllm",
    },
    "deepseek-v4-flash": {
        "context_length": 1_000_000,
        "max_output_length": 393_216,
        "features": ["tools", "json_mode", "structured_outputs"],
        "input_modalities": ["text"],
        "owned_by": "sglang",
    },
    "bge-m3": {
        "context_length": 8_192,
        "max_output_length": 0,
        "features": ["embeddings"],
        "input_modalities": ["text"],
        "owned_by": "sglang",
    },
}


class FreeInferenceCompletionConfig(CompletionConfigBase):
    """Serializable completion config that routes agent inference to FreeInference."""

    type: Literal["freeinference"] = "freeinference"
    base_url: str = FREEINFERENCE_BASE_URL
    api_key_env_var: str = FREEINFERENCE_API_KEY_ENV_VAR
    timeout_seconds: float = 300.0
    max_attempts: int = 5

    # Note: the installed Agents SDK (1.1.3) rejects output_type= with any custom
    # completion provider (UnsupportedAgentFeatureError at assembly), including
    # with adapter_capabilities.structured_output=True. Structured outputs are
    # therefore requested in the prompt and validated by the workflow activities
    # (see activities._parse_structured); FreeInference models declare
    # "structured_outputs" support for schema-following.

    def build_provider(self) -> CompletionProvider:
        api_key = os.environ.get(self.api_key_env_var)
        if not api_key:
            raise RuntimeError(
                f"Environment variable {self.api_key_env_var} is not set. "
                "Bind the workspace secret of the same name to the deployment, "
                "or export it locally."
            )
        return OpenAICompletionProvider(
            api_key=api_key,
            model=self.model,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            retry=RetryConfig(max_attempts=self.max_attempts, retry_after_max_wait_seconds=60),
        )


def completion_config_for_role(model: str, context_window: int, max_output_tokens: int) -> FreeInferenceCompletionConfig:
    return FreeInferenceCompletionConfig(
        model=model,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
    )
