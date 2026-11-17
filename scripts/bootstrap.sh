#!/usr/bin/env bash
# Bootstrap the independent FreeInference PR engineering runtime.
# Idempotent: safe to re-run. Requires Python 3.11+ and git.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v git >/dev/null; then
    echo "error: git is required" >&2
    exit 1
fi

if ! command -v uv >/dev/null; then
    echo "uv not found; installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "==> syncing dependencies into .venv"
uv sync

echo "==> running offline test suite"
uv run pytest -q

cat <<'EOF'

Bootstrap complete.

  source .venv/bin/activate
  cp .env.example .env        # then set FREEINFERENCE_API_KEY (and GITHUB_TOKEN if publishing)
  fi-pr doctor                # runtime health checks
  fi-pr smoke                 # real FreeInference integration check (needs the key)
  fi-pr --help

EOF
