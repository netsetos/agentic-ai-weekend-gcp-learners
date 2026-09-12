"""Mint a Google-signed ID token for a Cloud Run backend - the SLM (11.4) or the vLLM engine (11.1).

LiteLLM would send litellm_params.api_key as the bearer token, and Cloud Run IAM wants an ID token whose audience
is the service URL. Tokens last about an hour, so this refreshes a few minutes early rather than on 401 - a 401 inside
a fallback chain looks like the backend being down, and the router would helpfully route around a working service.
One cache entry per audience: the gateway fronts more than one backend (token_proxy.py).
"""
import os
import threading
import time

import google.auth.transport.requests
import google.oauth2.id_token

_LOCK = threading.Lock()
_CACHE: dict[str, dict] = {}          # audience -> {"token", "exp"}
REFRESH_MARGIN_S = 300


def get_id_token(audience: str | None = None) -> str:
    audience = (audience or os.environ.get("SLM_URL") or os.environ["GEMMA_VLLM_URL"]).removesuffix("/v1").rstrip("/")
    with _LOCK:
        hit = _CACHE.get(audience)
        if hit and time.time() < hit["exp"] - REFRESH_MARGIN_S:
            return hit["token"]
        req = google.auth.transport.requests.Request()
        token = google.oauth2.id_token.fetch_id_token(req, audience)
        _CACHE[audience] = {"token": token, "exp": time.time() + 3600}
        return token


if __name__ == "__main__":
    print(get_id_token()[:24] + "... (an ID token for the backend's audience)")
