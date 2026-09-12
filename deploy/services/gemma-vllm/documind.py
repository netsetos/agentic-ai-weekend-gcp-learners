from pydantic import BaseModel
from typing import Literal
from fastapi import APIRouter, Request, Depends
from vllm.sampling_params import SamplingParams, StructuredOutputsParams
from vllm.utils import random_uuid
import json
from auth import get_tenant  # get_tenant is defined in auth.py

router = APIRouter(prefix="/v1/documind", tags=["documind"])

class ClassificationResult(BaseModel):
    category: Literal["invoice","contract","report","letter","other"]
    confidence: float
    reasoning: str

# Request bodies: a scalar param binds as a QUERY string, but clients POST JSON.
# Wrap inputs in a model so FastAPI reads them from the request body.
class ClassifyRequest(BaseModel):
    text: str

class ExtractRequest(BaseModel):
    text: str
    schema_def: dict  # client-provided JSON Schema

@router.post("/classify")
async def classify_document(req: ClassifyRequest, request: Request, tenant: dict = Depends(get_tenant)):
    so = StructuredOutputsParams(json=ClassificationResult.model_json_schema())
    sampling = SamplingParams(temperature=0.0, max_tokens=256, structured_outputs=so)
    prompt = f"Classify this document into: invoice, contract, report, letter, other.\n\nDocument:\n{req.text[:4000]}\n\nRespond with JSON:"
    
    request_id = random_uuid()
    final = None
    async for output in request.app.state.engine.generate(prompt, sampling, request_id):
        final = output
    # Guided decoding GUARANTEES valid JSON matching schema
    return ClassificationResult.model_validate_json(final.outputs[0].text)

@router.post("/extract")
async def extract_fields(req: ExtractRequest, request: Request, tenant: dict = Depends(get_tenant)):
    so = StructuredOutputsParams(json=req.schema_def)
    sampling = SamplingParams(temperature=0.0, max_tokens=1024, structured_outputs=so)
    prompt = f"Extract structured data matching the schema.\n\nDocument:\n{req.text[:4000]}\n\nJSON:"
    
    request_id = random_uuid()
    final = None
    async for output in request.app.state.engine.generate(prompt, sampling, request_id):
        final = output
    return json.loads(final.outputs[0].text)
