import os
import streamlit as st
import jwt
from jwt import PyJWKClient

IAP_AUDIENCE = os.getenv("IAP_AUDIENCE")
ADMIN_EMAILS = set(os.getenv("ADMIN_EMAILS", "").split(","))
ADMIN_DOMAINS = set(os.getenv("ADMIN_DOMAINS", "").split(","))
_JWKS = PyJWKClient("https://www.gstatic.com/iap/verify/public_key-jwk")

def _verify_iap_jwt(token, audience):
    key = _JWKS.get_signing_key_from_jwt(token).key
    return jwt.decode(token, key, algorithms=["ES256"],
                      audience=audience, issuer="https://cloud.google.com/iap")

def current_user():
    if os.getenv("AUTH_MODE") == "iap":
        h = st.context.headers or {}
        token = h.get("x-goog-iap-jwt-assertion")
        if not token:
            return {"is_logged_in": False}
        try:
            claims = _verify_iap_jwt(token, IAP_AUDIENCE)
            return {"email": claims["email"], "sub": claims["sub"],
                    "is_logged_in": True}
        except Exception as e:
            st.error(f"IAP JWT verification failed: {e}")
            return {"is_logged_in": False}
    if st.user.is_logged_in:
        return {"email": st.user.email, "sub": st.user.sub,
                "name": getattr(st.user, "name", ""), "is_logged_in": True}
    return {"is_logged_in": False}

def login_gate():
    u = current_user()
    if not u["is_logged_in"]:
        st.title("🔐 DocuMind - Sign in required")
        if os.getenv("AUTH_MODE") != "iap":
            st.button("Log in with Google", on_click=st.login)
        else:
            st.error("IAP authentication missing. Contact admin.")
        st.stop()
    return u

@st.cache_data(ttl=300, show_spinner=False)
def tenant_for(email: str) -> str | None:
    """Which tenant does this person belong to?

    NOT user["sub"]. The version this replaced used the Google subject id as
    the tenant, which quietly made every user their own tenant: two colleagues
    at the same company could never see the same document, and rag-api's
    tenants/{tenant}/members/{email} roster was bypassed entirely.

    The member doc is keyed by email so rag-api can do a point lookup, and
    carries `email` as a field so this reverse lookup is one collection-group
    query rather than a scan of every tenant.
    """
    from google.cloud import firestore
    db = firestore.Client()
    hits = (db.collection_group("members")
              .where("email", "==", email.lower()).limit(1).get())
    for doc in hits:
        return doc.reference.parent.parent.id      # tenants/{THIS}/members/{email}
    return None


def is_admin(user):
    email = user.get("email", "").lower()
    domain = email.split("@")[-1] if "@" in email else ""
    return email in ADMIN_EMAILS or domain in ADMIN_DOMAINS
