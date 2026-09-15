"""The gateway's local door to Cloud Run backends that want a Google ID token per call. Module 11 (11.3, 11.4).

LiteLLM reads litellm_params.api_key once, at startup, from the environment. A Cloud Run ID token lives an hour. So
config.yaml points the self-hosted routes at http://127.0.0.1:8090/slm (and /vllm), and this process - started
beside litellm by entrypoint.sh - forwards every request to the real service with a token it mints and refreshes
itself (gcp_id_token.py, per audience). The token is never an environment variable and never pasted. Streams pass
through untouched, so Ollama's and vLLM's SSE reach LiteLLM as they were sent.
"""
from __future__ import annotations

import os

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from gcp_id_token import get_id_token

TARGETS = {"slm": os.environ.get("SLM_URL", "").rstrip("/"),
           "vllm": os.environ.get("GEMMA_VLLM_URL", "").rstrip("/")}
HOP = {"host", "authorization", "content-length", "connection", "transfer-encoding"}
client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0))


async def forward(request: Request):
    target, rest = request.path_params["target"], request.path_params["rest"]
    base = TARGETS.get(target)
    if not base:
        return Response(f"no backend configured for /{target} (SLM_URL / GEMMA_VLLM_URL)", status_code=502)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    headers["Authorization"] = f"Bearer {get_id_token(base)}"
    upstream = await client.send(client.build_request(request.method, f"{base}/{rest}", headers=headers,
                                                      content=await request.body(), params=request.query_params),
                                 stream=True)

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    passthrough = {k: v for k, v in upstream.headers.items() if k.lower() in ("content-type",)}
    return StreamingResponse(body(), status_code=upstream.status_code, headers=passthrough)


app = Starlette(routes=[Route("/{target}/{rest:path}", forward, methods=["GET", "POST", "DELETE"])])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("TOKEN_PROXY_PORT", "8090")), log_level="warning")
