"""Who is asking, verified.

IAP puts a signed JWT in x-goog-iap-jwt-assertion. Reading x-goog-authenticated-user-email
without verifying the signature is the bug 12.2 removed from rag-api: a header is a claim,
not a credential, and anything that can reach the service can set one.
"""
import os

import streamlit as st
from google.auth.transport import requests as g_requests
from google.oauth2 import id_token

IAP_AUDIENCE = os.environ.get("IAP_AUDIENCE", "")
IAP_CERTS = "https://www.gstatic.com/iap/verify/public_key"
ADMIN_EMAILS = [e.strip().lower() for e in
                os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()]


def current_user() -> dict:
    if not IAP_AUDIENCE:
        # Fail closed. Verifying without an audience accepts a token minted for
        # ANY service behind IAP in this project.
        st.error("IAP_AUDIENCE is not set; refusing to authenticate.")
        st.stop()
    token = st.context.headers.get("x-goog-iap-jwt-assertion")
    if not token:
        st.error("403 - reach this service through IAP.")
        st.stop()
    try:
        claims = id_token.verify_token(token, g_requests.Request(),
                                       audience=IAP_AUDIENCE, certs_url=IAP_CERTS)
    except Exception:
        st.error("403 - invalid IAP assertion.")
        st.stop()
    email = (claims.get("email") or "").lower()
    # The IAP group binding is the gate; this is defence in depth, and it is the
    # check that still holds if somebody widens the group by accident.
    if ADMIN_EMAILS and email not in ADMIN_EMAILS:
        st.error("403 - Admins only")
        st.stop()
    return {"email": email, "sub": claims.get("sub")}
