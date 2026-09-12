import asyncio, json, time
from fastapi import Request

async def generate_sse_stream(engine, request_id, model_name, prompt, sampling_params, request: Request):
    """Convert vLLM RequestOutput stream to OpenAI SSE format."""
    created = int(time.time())
    first_chunk = True
    prev_len = {}  # completion index -> chars already sent
    try:
        async for output in engine.generate(prompt, sampling_params, request_id):
            # Client disconnected? Abort and free GPU
            if await request.is_disconnected():
                await engine.abort(request_id)
                return
            
            for completion in output.outputs:
                if first_chunk:
                    chunk = {"id": request_id, "object": "chat.completion.chunk",
                             "created": created, "model": model_name,
                             "choices": [{"index": 0,
                                "delta": {"role": "assistant", "content": ""},
                                "finish_reason": None}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
                    first_chunk = False
                
                # vLLM completion.text is CUMULATIVE -> emit only the new suffix
                idx = completion.index
                sent = prev_len.get(idx, 0)
                new_text = completion.text[sent:]
                prev_len[idx] = len(completion.text)
                if new_text:
                    chunk = {"id": request_id, "object": "chat.completion.chunk",
                             "created": created, "model": model_name,
                             "choices": [{"index": 0,
                                "delta": {"content": new_text},
                                "finish_reason": None}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
                
                if completion.finish_reason is not None:
                    chunk = {"id": request_id, "object": "chat.completion.chunk",
                             "created": created, "model": model_name,
                             "choices": [{"index": 0, "delta": {},
                                "finish_reason": completion.finish_reason}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
        
        yield "data: [DONE]\n\n"
    except asyncio.CancelledError:
        await engine.abort(request_id)
        raise
