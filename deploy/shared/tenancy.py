"""One tenant roster, read one way. Lesson 12.8.

The roster is Firestore, at `tenants/{tenant_id}/members/{email}`. Three surfaces need it and
each had arrived at its own version:

    rag-api/auth.py       is_member(email, tenant) - a POINT lookup. It is given the tenant and
                          asks "is this person in it?". Right for a service that receives a
                          tenant_id on the request and must authorise it.
    frontend/auth.py      tenant_for(email) - a REVERSE lookup by collection group. It is given
                          a person and asks "which tenant?". Right for a browser surface where
                          nobody sends a tenant at all.
    chat/agent.py         neither. It believed the request body.

Both directions are legitimate and both are here, so the next surface picks one instead of
writing a third. The member document is keyed by EMAIL so the point lookup is a single read,
and carries `email` as a FIELD so the reverse lookup is one collection-group query rather than
a scan of every tenant - that shape is load-bearing for both functions and is why neither is
implemented in terms of the other.

WHY A PERSON HAS ONE TENANT HERE. The reverse lookup returns the first match. DocuMind's model
is that a person belongs to one customer; a consultant working for two would need this to
return a list and every caller to choose, which is a product decision, not a code change. Said
out loud because "it returns the first row" is otherwise indistinguishable from a bug.
"""
from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def _db():
    from google.cloud import firestore
    return firestore.Client()


def is_member(email: str, tenant_id: str) -> bool:
    """Is this person on THIS tenant's roster? One document read."""
    if not email or not tenant_id:
        return False
    doc = (_db().collection("tenants").document(tenant_id)
           .collection("members").document(email.lower()).get())
    return doc.exists


def tenant_for(email: str) -> str | None:
    """Which tenant does this person belong to? None if nobody's.

    None is the honest answer for a verified user who is on no roster, and the caller
    should turn it into a 403 - they are authenticated and not authorised. Returning a
    default tenant here would be the same class of bug as trusting a header.
    """
    if not email:
        return None
    hits = (_db().collection_group("members")
            .where("email", "==", email.lower()).limit(1).get())
    for doc in hits:
        return doc.reference.parent.parent.id      # tenants/{THIS}/members/{email}
    return None


# ---- the write side, for the operator ----------------------------------------------------
# Nothing above WRITES the roster; a service that could would be a service that could enrol
# itself. Membership is an operator's act - `make roster` in deploy/ runs this - and the
# document shape is exactly what the two readers above depend on: keyed by email, with
# `email` as a field.

def add_member(tenant_id: str, email: str) -> None:
    """Put a person on a tenant's roster. Idempotent: the same document, the same fields."""
    from google.cloud import firestore
    (_db().collection("tenants").document(tenant_id)
         .collection("members").document(email.lower())
         .set({"email": email.lower(), "added_at": firestore.SERVER_TIMESTAMP}))


def list_members(tenant_id: str) -> list[str]:
    return sorted(d.id for d in _db().collection("tenants").document(tenant_id)
                  .collection("members").stream())


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="the tenant roster, from the operator's side")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="put a person (or a service account) on a tenant's roster")
    a.add_argument("tenant"); a.add_argument("email")
    l = sub.add_parser("list", help="who is on a tenant's roster")
    l.add_argument("tenant")
    args = ap.parse_args()
    if args.cmd == "add":
        add_member(args.tenant, args.email)
        print(f"{args.email.lower()} is on {args.tenant}")
    else:
        for m in list_members(args.tenant):
            print(m)
