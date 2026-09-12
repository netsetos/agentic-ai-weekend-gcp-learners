"""IAP identity and tenant membership for the DocuMind RAG API.

The tenant is something you ARE, not something you send. The previous version
read x-user-email and x-tenant-id straight off the request, so anyone who could
reach the service could claim any tenant - and Cloud Run services are reachable
from more places than people expect.

Since 12.8 the verifier itself is shared/iap.py - one implementation for four
surfaces - and it has two legs (gap G4, 2026-09-05):

    x-goog-iap-jwt-assertion   the PERSON, forwarded by the surface they signed
                               in to (documind-ui, documind-chat). Accepted for
                               every audience in IAP_AUDIENCE, and only those.
    Authorization: Bearer      the CALLER's own Google ID token, minted for
                               SELF_URL (lesson 7.3): an agent in a notebook,
                               run_eval.py, smoke.py. No person, so no
                               assertion; the account itself must be on the
                               tenant's roster.

The assertion wins when both are present. Nothing else is read.
"""
import os

from fastapi import HTTPException, Request

from shared import iap
from shared.tenancy import is_member

AUTH_MODE = os.environ.get("AUTH_MODE", "iap")        # iap | dev
# This service's own URL - the audience every caller mints an ID token for.
# Unset means the bearer leg is off and only forwarded assertions are accepted.
SELF_URL = os.environ.get("SELF_URL", "")


def verify_iap(request: Request) -> dict:
    """Return {"email": ..., "via": iap|iam|dev} for the caller, or raise 401."""
    if AUTH_MODE == "dev":
        # Local and test only. Set AUTH_MODE=iap everywhere else; run-service.yaml
        # does exactly that, so dev mode cannot reach prod by accident.
        email = request.headers.get("x-user-email")
        if not email:
            raise HTTPException(401, "missing identity headers")
        return {"email": email.lower(), "via": "dev"}
    try:
        return iap.identity(request.headers, bearer_audience=SELF_URL or None)
    except iap.IapError as e:
        # 401, not 403: we do not know who this is. This also fails closed on a
        # missing IAP_AUDIENCE - verifying without an audience accepts a token
        # minted for ANY service.
        raise HTTPException(401, str(e))


def enforce_membership(email: str, tenant_id: str) -> None:
    """403 on mismatch - a real authorisation check, not a header comparison.

    One roster, read one way: shared/tenancy.py, the same document the frontend
    and the chat service consult, so a membership change lands everywhere at once.
    """
    if not is_member(email, tenant_id):
        raise HTTPException(403, "not a member of this tenant")
