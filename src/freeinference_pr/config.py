"""Environment-driven configuration for the independent FreeInference runtime.

No Mistral credentials are required. Every value comes from the environment with
a safe default; secrets are never stored here, only read from the environment at
use time.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_BASE_URL = "https://freeinference.org/v1"

# Verified against GET /models on 2026-11-16. Overridable per role via env.
DEFAULT_ORCHESTRATOR_MODEL = "qwen3.6-35b"
DEFAULT_IMPLEMENTER_MODEL = "deepseek-v4-flash"
DEFAULT_REVIEWER_MODEL = "qwen3.6-35b"
DEFAULT_ANALYST_MODEL = "deepseek-v4-flash"
DEFAULT_SECURITY_MODEL = "qwen3.6-35b"
DEFAULT_VERIFIER_MODEL = "deepseek-v4-flash"
DEFAULT_INTEGRATION_MODEL = "qwen3.6-35b"
DEFAULT_PACKAGER_MODEL = "qwen3.6-35b"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


class FIConfig:
    """Resolved runtime configuration (inference, limits, paths, GitHub)."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        if env is not None:
            self._patch(env)

    def _patch(self, env: dict[str, str]) -> None:
        self._env_overlay = env

    def get(self, name: str, default: str = "") -> str:
        overlay = getattr(self, "_env_overlay", None)
        if overlay is not None:
            return overlay.get(name, os.environ.get(name, default)).strip() or default
        return os.environ.get(name, "").strip() or default

    # --- inference ---------------------------------------------------------
    @property
    def api_key(self) -> str:
        return self.get("FREEINFERENCE_API_KEY", "")

    @property
    def base_url(self) -> str:
        return self.get("FREEINFERENCE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")

    @property
    def max_concurrent(self) -> int:
        return max(1, self.get_int("FI_MAX_CONCURRENT", 2))

    @property
    def request_timeout(self) -> float:
        return self.get_float("FI_REQUEST_TIMEOUT", 300.0)

    @property
    def max_attempts(self) -> int:
        return max(1, self.get_int("FI_MAX_ATTEMPTS", 5))

    @property
    def backoff_base_seconds(self) -> float:
        return self.get_float("FI_BACKOFF_BASE", 1.0)

    @property
    def backoff_max_seconds(self) -> float:
        return self.get_float("FI_BACKOFF_MAX", 30.0)

    # --- models per role ----------------------------------------------------
    @property
    def role_models(self) -> dict[str, str]:
        return {
            "orchestrator": self.get("FI_ORCHESTRATOR_MODEL", DEFAULT_ORCHESTRATOR_MODEL),
            "repository_analyst": self.get("FI_ANALYST_MODEL", DEFAULT_ANALYST_MODEL),
            "implementer": self.get("FI_IMPLEMENTER_MODEL", DEFAULT_IMPLEMENTER_MODEL),
            "code_reviewer": self.get("FI_REVIEWER_MODEL", DEFAULT_REVIEWER_MODEL),
            "security_reviewer": self.get("FI_SECURITY_MODEL", DEFAULT_SECURITY_MODEL),
            "verifier": self.get("FI_VERIFIER_MODEL", DEFAULT_VERIFIER_MODEL),
            "integrator": self.get("FI_INTEGRATION_MODEL", DEFAULT_INTEGRATION_MODEL),
            "pr_manager": self.get("FI_PACKAGER_MODEL", DEFAULT_PACKAGER_MODEL),
        }

    def model_for(self, role: str) -> str:
        return self.role_models[role]

    # --- paths ---------------------------------------------------------------
    @property
    def state_path(self) -> Path:
        return Path(self.get("FI_STATE_PATH", "state/freeinference-pr.db"))

    @property
    def workspace_root(self) -> Path:
        return Path(self.get("FI_WORKSPACE_ROOT", "workspaces"))

    # --- github ---------------------------------------------------------------
    @property
    def github_token(self) -> str:
        return self.get("GITHUB_TOKEN", "")

    @property
    def github_api(self) -> str:
        return self.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")

    # --- limits ----------------------------------------------------------------
    @property
    def task_timeout_seconds(self) -> float:
        return self.get_float("FI_TASK_TIMEOUT", 1800.0)

    @property
    def heartbeat_stale_seconds(self) -> float:
        return self.get_float("FI_HEARTBEAT_STALE", 600.0)

    @property
    def max_correction_cycles(self) -> int:
        return max(0, self.get_int("FI_MAX_CORRECTION_CYCLES", 3))

    @property
    def max_agent_turns(self) -> int:
        return max(1, self.get_int("FI_MAX_AGENT_TURNS", 12))

    def get_int(self, name: str, default: int) -> int:
        try:
            return int(self.get(name, str(default)))
        except ValueError:
            return default

    def get_float(self, name: str, default: float) -> float:
        try:
            return float(self.get(name, str(default)))
        except ValueError:
            return default
