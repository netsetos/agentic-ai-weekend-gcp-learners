# main.py - Production FastAPI + vLLM server
from contextlib import asynccontextmanager
import logging, os

from fastapi import BackgroundTasks, Depends, FastAPI, Request
from fastapi.responses import StreamingResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from transformers import AutoTokenizer
from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.engine.async_llm_engine import AsyncLLMEngine
from vllm.sampling_params import SamplingParams
from vllm.utils import random_uuid

from auth import get_tenant, limiter, tier_rate_limit
from documind import router as documind_router
from logging_module import log_request, redact_pii
from schemas import (AssistantMessage, ChatCompletionChoice,
                     ChatCompletionRequest, ChatCompletionResponse, UsageInfo)
from streaming import generate_sse_stream

logger = logging.getLogger("documind")
MODEL_NAME = os.getenv("MODEL_NAME", "google/gemma-3-4b-it")
# vLLM reports "abort" and "length"; OpenAI clients only understand this set.
_FINISH = {"stop", "length", "tool_calls", "content_filter"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Loading model into GPU...")
    engine_args = AsyncEngineArgs(
        model=MODEL_NAME,
        tensor_parallel_size=1,
        gpu_memory_utilization=0.90,
        max_model_len=8192,
        dtype="auto",
        trust_remote_code=True,
    )
    app.state.engine = AsyncLLMEngine.from_engine_args(engine_args)
    # The chat template ships WITH the checkpoint. Load the tokenizer once here -
    # doing it per request costs ~200ms and defeats the point of a warm instance.
    app.state.tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    app.state.engine_ready = True
    logger.info("Engine loaded. GPU ready.")
    yield
    app.state.engine_ready = False
    app.state.engine = None


app = FastAPI(title="DocuMind AI Inference", version="1.0.0", lifespan=lifespan)

# ---- wiring order matters -------------------------------------------------
# 1. limiter onto app.state FIRST - @limiter.limit reads it off the app at call time
# 2. the RateLimitExceeded handler, or a tripped limit surfaces as a 500 instead of a 429
# 3. SlowAPIMiddleware last
# Get this order wrong and nothing fails at import; it fails on the first rate-limited
# request, in production, as a 500.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

FastAPIInstrumentor.instrument_app(app)
app.include_router(documind_router)


@app.get("/health")
async def health():
    """Readiness, not liveness: 200 only once the weights are on the GPU."""
    ready = getattr(app.state, "engine_ready", False)
    return {"status": "ok" if ready else "loading", "model": MODEL_NAME}


def build_prompt(tokenizer, messages) -> str:
    """Apply the checkpoint's OWN chat template.

    Gemma 3 is instruction-tuned and expects <start_of_turn>user ... <end_of_turn>.
    Concatenating the message contents by hand still returns text, so this is the
    kind of bug that ships: quality quietly drops and the model talks past its turn.
    """
    return tokenizer.apply_chat_template(
        [{"role": m.role, "content": m.content or ""} for m in messages],
        tokenize=False,
        add_generation_prompt=True,
    )


@app.post("/v1/chat/completions")
@limiter.limit(tier_rate_limit)
async def chat_completions(
    req: ChatCompletionRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    tenant: dict = Depends(get_tenant),
):
    request.state.tenant_tier = tenant["tier"]
    request_id = random_uuid()
    prompt = build_prompt(request.app.state.tokenizer, req.messages)
    sampling = SamplingParams(temperature=req.temperature, top_p=req.top_p,
                              max_tokens=req.max_tokens or 512, stop=req.stop)

    if req.stream:
        return StreamingResponse(
            generate_sse_stream(request.app.state.engine, request_id,
                                req.model, prompt, sampling, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    final = None
    async for output in request.app.state.engine.generate(prompt, sampling, request_id):
        final = output
    completion = final.outputs[0]
    usage = UsageInfo(
        prompt_tokens=len(final.prompt_token_ids),
        completion_tokens=len(completion.token_ids),
        total_tokens=len(final.prompt_token_ids) + len(completion.token_ids),
    )
    background_tasks.add_task(log_request, {
        "request_id": request_id,
        "tenant_id": tenant["tenant_id"],
        "model": req.model,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "prompt_redacted": redact_pii(prompt[:2000]),
    })
    return ChatCompletionResponse(
        id=f"chatcmpl-{request_id}",
        model=req.model,
        choices=[ChatCompletionChoice(
            index=0,
            message=AssistantMessage(role="assistant", content=completion.text),
            finish_reason=completion.finish_reason
            if completion.finish_reason in _FINISH else "stop",
        )],
        usage=usage,
    )
