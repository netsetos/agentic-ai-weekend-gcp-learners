"""One IAP verifier, for every surface. Lesson 12.8.

WHY THIS FILE EXISTS. DocuMind had FOUR surfaces and four different answers to "who is asking":

    documind-api    rag-api/auth.py     google.oauth2.id_token.verify_token, certs_url=
                                        .../iap/verify/public_key, fails closed on a missing
                                        audience, then a Firestore roster check.
    documind-admin  admin/auth.py       the same library and endpoint, fails closed the same
                                        way, then an ADMIN_EMAILS allowlist.
    documind-ui     frontend/auth.py    a DIFFERENT library - PyJWT's PyJWKClient against
                                        .../iap/verify/public_key-jwk, algorithms=["ES256"],
                                        explicit issuer - and it does NOT fail closed when
                                        IAP_AUDIENCE is unset.
    documind-chat   chat/agent.py       nothing at all. tenant_id and user_id arrive in the
                                        request body, so the caller picks the tenant.

None of the first three is wrong on its own. The problem is that there are three: a fix now has
to be made three times, and it will be made once. That is the same argument deploy/UNOWNED.md
already makes about shared/pii.py - "two lists that drift is the worst kind of compliance bug:
the scan misses a type, the dashboard reports zero findings, and both look correct". Swap "list"
for "verifier".

WHAT VERIFICATION HAS TO ESTABLISH, and all four must hold:

    signature   signed by Google IAP, against the IAP key set - not the general OAuth certs.
    audience    minted for THIS service. Skip it and a token for ANY IAP-protected service in
                the project is accepted. This is the check people leave out.
    issuer      https://cloud.google.com/iap
    expiry      still valid. The library does this for you if you let it.

Reading `x-goog-authenticated-user-email` instead is not a shortcut, it is the absence of a
check: anything that can reach the service can set a header.

AUDIENCE, PRECISELY. For a Cloud Run service with IAP enabled directly, the audience is

    /projects/PROJECT_NUMBER/locations/REGION/services/SERVICE_NAME

Note the leading slash and the PROJECT NUMBER, not the project id. Getting this wrong produces
a permanent 401 that looks exactly like a broken login.

ACCEPT_AUDIENCES IS A LIST, ON PURPOSE. documind-api is not behind IAP - nothing reaches it
from a browser - so the assertion it sees was minted for whichever SURFACE forwarded it, and
there is more than one (documind-ui and documind-chat). A service that accepts forwarded
assertions must name every audience it will accept, and must accept no others.
"""
from __future__ import annotations

import os
from functools import lru_cache

# google-auth is imported INSIDE verify(), not here. The allowlist and audience logic below
# is where the bugs were, and it should be testable without a cloud SDK on the machine -
# a security helper nobody can unit-test is a security helper nobody unit-tests.

# IAP signs with its OWN key set. The general Google OAuth certs will not verify these.
IAP_CERTS = "https://www.gstatic.com/iap/verify/public_key"
IAP_ISSUER = "https://cloud.google.com/iap"
ASSERTION_HEADER = "x-goog-iap-jwt-assertion"


class IapError(Exception):
    """Verification failed. The caller decides whether that is a 401 or a 403."""


@lru_cache(maxsize=1)
def accepted_audiences() -> tuple[str, ...]:
    """Every audience this service will accept, from IAP_AUDIENCE (comma-separated).

    Empty is FAIL CLOSED, not "accept anything". frontend/auth.py used to pass the unset
    value straight to the library and rely on whatever it did with audience=None - a
    security property nobody had decided, resolved by a dependency's default.
    """
    raw = os.environ.get("IAP_AUDIENCE", "")
    return tuple(a.strip() for a in raw.split(",") if a.strip())


def verify(assertion: str | None) -> dict:
    """Return the verified claims, or raise IapError. Never returns unverified data."""
    auds = accepted_audiences()
    if not auds:
        raise IapError("IAP_AUDIENCE is not configured; refusing to authenticate")
    if not assertion:
        raise IapError(f"no {ASSERTION_HEADER} - this request did not come through IAP")

    from google.auth.transport import requests as g_requests
    from google.oauth2 import id_token

    last = None
    for aud in auds:
        try:
            claims = id_token.verify_token(
                assertion, g_requests.Request(), audience=aud, certs_url=IAP_CERTS)
        except Exception as e:            # noqa: BLE001 - any failure is a refusal
            last = e
            continue
        if claims.get("iss") != IAP_ISSUER:
            raise IapError(f"issuer is {claims.get('iss')!r}, not IAP")
        email = (claims.get("email") or "").lower()
        if not email:
            raise IapError("the assertion carries no email")
        return {"email": email, "sub": claims.get("sub"), "aud": aud}
    raise IapError(f"invalid assertion: {type(last).__name__ if last else 'no audience matched'}")


def email_from(headers) -> str:
    """The verified caller's email, from a mapping of request headers.

    Accepts anything with .get() - FastAPI's request.headers and Streamlit's
    st.context.headers both qualify, which is why this signature and not a framework type.
    """
    return verify(headers.get(ASSERTION_HEADER))["email"]


def parse_allowlist(raw: str | None) -> frozenset[str]:
    """Split a comma-separated allowlist, DROPPING empties.

    This exists because of a real bug. frontend/auth.py did

        ADMIN_EMAILS = set(os.getenv("ADMIN_EMAILS", "").split(","))

    and ''.split(',') is [''], not []. So an unset allowlist contained the empty string, and
    is_admin() returned True for any user whose email was missing - an unset allowlist meaning
    "anybody without an email" instead of "nobody". admin/auth.py parsed the same variable with
    `if e.strip()` and was correct. One function, so there is one behaviour.
    """
    return frozenset(e.strip().lower() for e in (raw or "").split(",") if e.strip())


def is_allowed(email: str, emails: frozenset[str], domains: frozenset[str] = frozenset()) -> bool:
    """Membership of an allowlist. An EMPTY allowlist allows nobody."""
    email = (email or "").lower()
    if not email or "@" not in email:
        return False                      # no identity is not a match, it is a refusal
    if not emails and not domains:
        return False
    return email in emails or email.rsplit("@", 1)[-1] in domains


# ---------------------------------------------------------------------------- the other leg
# Gap G4 (2026-09-05). Not every caller has a person behind it. An agent in a notebook,
# run_eval.py, smoke.py: nobody signed in, so IAP minted nothing. What they DO have is the
# Google ID token they minted for this service's URL (lesson 7.3) - the same token Cloud Run's
# IAM ingress checked roles/run.invoker on. Re-verifying it here is what lets the email inside
# be trusted as the caller. It establishes WHO, not WHETHER: the account still has to be on the
# tenant's roster (shared/tenancy.py).


def bearer_email(headers, audience: str) -> str:
    """The identity behind `Authorization: Bearer <Google ID token>`, verified for `audience`."""
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        raise IapError("no Authorization: Bearer token")
    if not audience:
        raise IapError("SELF_URL is not configured; refusing to verify bearer tokens")

    from google.auth.transport import requests as g_requests
    from google.oauth2 import id_token

    try:
        claims = id_token.verify_oauth2_token(auth[7:].strip(), g_requests.Request(),
                                              audience=audience)
    except Exception as e:                # noqa: BLE001 - any failure is a refusal
        raise IapError(f"invalid bearer token: {type(e).__name__}")
    email = (claims.get("email") or "").lower()
    if not email or not claims.get("email_verified", True):
        raise IapError("the bearer token carries no verified email")
    return email


def identity(headers, *, bearer_audience: str | None = None) -> dict:
    """Who is this request for? The assertion first, the bearer token second, never a header.

    A surface (documind-ui, documind-chat) forwards the person's IAP assertion beside its own
    ID token. The assertion wins because it names the PERSON; the token only proves the surface
    was allowed to call. With no assertion and a bearer audience configured, the token's own
    identity is the caller. With neither, refuse - there is nobody to be.
    """
    assertion = headers.get(ASSERTION_HEADER)
    if assertion:
        claims = verify(assertion)
        return {"email": claims["email"], "via": "iap", "aud": claims["aud"]}
    if bearer_audience:
        return {"email": bearer_email(headers, bearer_audience), "via": "iam",
                "aud": bearer_audience}
    raise IapError(f"no {ASSERTION_HEADER} and no bearer audience configured - refusing")
