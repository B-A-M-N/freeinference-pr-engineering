"""Secret redaction for traces, logs, tool results, and CLI output."""

from __future__ import annotations

import re

_PATTERNS = [
    re.compile(r"hyi-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gho_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)(api[_-]?key|secret|token|password)[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._\-]{12,}"),
]

_REPLACEMENT = "[REDACTED]"


def redact(text: str) -> str:
    if not text:
        return text
    result = text
    for pattern in _PATTERNS:
        result = pattern.sub(_REPLACEMENT, result)
    return result


def redact_obj(value: object) -> object:
    """Recursively redact strings inside dicts/lists; returns a redacted copy."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    return value
