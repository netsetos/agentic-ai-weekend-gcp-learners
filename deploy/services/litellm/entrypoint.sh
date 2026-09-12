#!/bin/sh
# The gateway container runs two processes: the token proxy (a local door to the Cloud Run backends, an ID token per
# call) and LiteLLM itself. LITELLM_CONFIG picks the profile's config: config.lean.yaml (no database, IAM at the door)
# or config.yaml (the full profile: Postgres, master key, tag budgets).
set -e
python /app/token_proxy.py &
CONFIG="/app/${LITELLM_CONFIG:-config.lean.yaml}"
if command -v litellm >/dev/null 2>&1; then
  exec litellm --config "$CONFIG" --port "${PORT:-8080}" --host 0.0.0.0
fi
exec python -m litellm.proxy.proxy_cli --config "$CONFIG" --port "${PORT:-8080}" --host 0.0.0.0
