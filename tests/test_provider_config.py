"""Tests for the FreeInference completion config (no network)."""

import json

import pytest

from freeinference_pr.models_catalog import ROLE_PRESETS, completion_for_role
from freeinference_pr.provider import (
    FREEINFERENCE_API_KEY_ENV_VAR,
    FREEINFERENCE_BASE_URL,
    VERIFIED_MODEL_CATALOG,
    FreeInferenceCompletionConfig,
)


def test_config_serializes_without_secret():
    config = completion_for_role("investigation")
    payload = config.model_dump_json()
    data = json.loads(payload)
    assert data["base_url"] == FREEINFERENCE_BASE_URL
    assert data["api_key_env_var"] == FREEINFERENCE_API_KEY_ENV_VAR
    assert "sk" not in payload and "hyi-" not in payload


def test_config_type_registers_class():
    config = completion_for_role("review")
    assert config.type == "freeinference"
    assert FreeInferenceCompletionConfig.model_fields["type"].default == "freeinference"


def test_build_provider_requires_env_var(monkeypatch):
    monkeypatch.delenv(FREEINFERENCE_API_KEY_ENV_VAR, raising=False)
    config = completion_for_role("orchestrator")
    with pytest.raises(RuntimeError, match=FREEINFERENCE_API_KEY_ENV_VAR):
        config.build_provider()


def test_build_provider_uses_env_var(monkeypatch):
    monkeypatch.setenv(FREEINFERENCE_API_KEY_ENV_VAR, "hyi-test-value")
    config = completion_for_role("orchestrator")
    provider = config.build_provider()
    assert provider is not None


def test_role_presets_use_verified_models():
    for role, preset in ROLE_PRESETS.items():
        assert preset.model in VERIFIED_MODEL_CATALOG
        catalog = VERIFIED_MODEL_CATALOG[preset.model]
        assert "tools" in catalog["features"]
        assert "structured_outputs" in catalog["features"]
        assert preset.context_window <= catalog["context_length"]


def test_review_model_differs_from_implementation():
    assert ROLE_PRESETS["review"].model != ROLE_PRESETS["implementation"].model
