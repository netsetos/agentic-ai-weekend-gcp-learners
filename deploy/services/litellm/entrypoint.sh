#!/bin/sh
# The gateway container runs two processes: the token proxy (a local door to the Cloud Run backends, an ID token per
# call) and LiteLLM itself, on the one config (IAM at the door, the token proxy, Postgres for the spend logs).
set -e
python /app/token_proxy.py &
CONFIG="/app/config.yaml"
if command -v litellm >/dev/null 2>&1; then
  exec litellm --config "$CONFIG" --port "${PORT:-8080}" --host 0.0.0.0
fi
exec python -m litellm.proxy.proxy_cli --config "$CONFIG" --port "${PORT:-8080}" --host 0.0.0.0
