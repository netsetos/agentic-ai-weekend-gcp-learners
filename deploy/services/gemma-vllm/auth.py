import hashlib
import datetime
import os
from functools import lru_cache
from fastapi import Depends, HTTPException, Security, Request
from fastapi.security import APIKeyHeader
from slowapi import Limiter
from google.cloud import firestore

api_key_header = APIKeyHeader(name="X-API-Key")
firestore_client = firestore.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT"))

def hash_api_key(plaintext: str) -> str:
    """SHA-256 for lookup; for production use bcrypt with salt."""
    return hashlib.sha256(plaintext.encode()).hexdigest()

async def get_tenant(api_key: str = Security(api_key_header)) -> dict:
    """Lookup tenant by HASHED key. Supports key rotation via multiple active keys."""
    key_hash = hash_api_key(api_key)
    doc = firestore_client.collection("api_keys").document(key_hash).get()
    if not doc.exists:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    data = doc.to_dict()
    # Check expiry for key rotation
    if data.get("expires_at") and data["expires_at"] < datetime.datetime.utcnow():
        raise HTTPException(status_code=401, detail="Expired API key")
    
    # Fetch tenant details
    tenant_doc = firestore_client.collection("tenants").document(data["tenant_id"]).get()
    return tenant_doc.to_dict()  # {tenant_id, tier, rate_limit}

def rate_limit_key(request: Request):
    return request.headers.get("X-API-Key", "anonymous")

limiter = Limiter(key_func=rate_limit_key)

TIER_LIMITS = {"free": "10/minute", "pro": "100/minute", "enterprise": "1000/minute"}

@lru_cache(maxsize=1024)
def tier_for_key(key_hash: str) -> str:
    """Firestore lookup, memoised - the limiter runs on EVERY request."""
    doc = firestore_client.collection("api_keys").document(key_hash).get()
    if not doc.exists:
        return "free"
    tenant = firestore_client.collection("tenants").document(
        doc.to_dict()["tenant_id"]).get()
    return (tenant.to_dict() or {}).get("tier", "free")

def tier_rate_limit(key: str) -> str:
    """The parameter MUST be named `key`.

    slowapi inspects this signature: a parameter named `key` makes it call
    tier_rate_limit(key_func(request)); any other name and it calls tier_rate_limit()
    with no arguments and raises TypeError at request time. Note also that the tier
    cannot come from request.state - the limit is evaluated BEFORE the handler body
    runs, so it has to be resolved from the key itself.
    """
    if key == "anonymous":
        return TIER_LIMITS["free"]
    return TIER_LIMITS.get(tier_for_key(hash_api_key(key)), "10/minute")
