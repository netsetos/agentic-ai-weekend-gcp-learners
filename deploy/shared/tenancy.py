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

THE DATA-REGION POLICY (13 September 2026, evening). The roster says who may read a tenant's documents;
`tenant_settings/{tenant}.data_region` says where the platform may HOLD them - `in` (the kit's own rows in
asia-south1 and nothing else) or `any` (a managed store outside India may keep a copy: RAG Engine in
us-central1, Vertex AI Search in global - services/ingest/managed.py mirrors current versions there, and
rag-api reads them as a retrieval backend). Absent means `in`: fail closed. It is read here by the two
services (policy_for, permits) and written only by the operator (set_policy, `make tenant-policy`), for
the same reason membership is: a service that could widen its own tenant's region would be a service that
could enrol itself. Residency stopped being a deployment variable that day (managed-retrieval-plan
2026-09-13.md, section 7) and became this document, one field per tenant.
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


# ---- the data-region policy (13 September 2026, evening) ---------------------------------
# Where a tenant's text may be held, per tenant, as data. tenant_settings/{tenant} is the
# document 11.4's pin already reads once a minute (model_backend, generator_model); the policy
# is one more field of it, so a tenant is one document to an operator and one read to a
# service. A store declares the region it holds data in (RagEngineStore.region, Vertex
# SearchStore.region in services/ingest/managed.py); permits() is the whole rule, so a Mumbai
# RAG Engine region one day changes nothing here.

DATA_REGIONS = ("in", "any")                  # in: never leaves India; any: a managed store may hold a copy
INDIA_REGION_PREFIXES = ("asia-south",)       # asia-south1 Mumbai, asia-south2 Delhi


def policy_of(doc: dict | None) -> str:
    """The data_region a tenant_settings document declares - `in` unless it says `any`. The one normalisation both
    readers use: a missing field, an unknown value or no document at all is `in`, never a guess."""
    region = str((doc or {}).get("data_region") or "in").strip().lower()
    return region if region in DATA_REGIONS else "in"


def policy_for(tenant_id: str, db=None) -> str:
    """The tenant's data_region, read from Firestore (the worker passes its own client; the gate a fake). Fail closed:
    a failed read is `in` too - an unreadable policy is the strict one, not the permissive one."""
    if not tenant_id:
        return "in"
    try:
        snap = (db or _db()).collection("tenant_settings").document(tenant_id).get()
        return policy_of((snap.to_dict() or {}) if snap.exists else {})
    except Exception:  # noqa: BLE001 - the policy is a guard; when it cannot be read nothing leaves
        return "in"


def permits(policy: str, region: str | None) -> bool:
    """May a store in `region` hold text under this policy? `any` permits every store; `in` permits a store inside
    India only - none of the managed stores is there today (us-central1, global), and the rule is data, not code."""
    if policy == "any":
        return True
    return str(region or "").strip().lower().startswith(INDIA_REGION_PREFIXES)


def set_policy(tenant_id: str, region: str) -> str:
    """The operator's write: tenant_settings/{tenant}.data_region, the pin's other fields kept (merge). Refuses
    anything but in | any - a typo must not widen a tenant's region by falling through to a default."""
    from google.cloud import firestore
    region = (region or "").strip().lower()
    if region not in DATA_REGIONS:
        raise ValueError(f"data_region={region!r}: one of {'|'.join(DATA_REGIONS)}")
    (_db().collection("tenant_settings").document(tenant_id)
         .set({"data_region": region, "data_region_set_at": firestore.SERVER_TIMESTAMP}, merge=True))
    return region


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="the tenant roster, from the operator's side")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="put a person (or a service account) on a tenant's roster")
    a.add_argument("tenant"); a.add_argument("email")
    l = sub.add_parser("list", help="who is on a tenant's roster")
    l.add_argument("tenant")
    p = sub.add_parser("policy", help="where a tenant's text may be held: in | any (make tenant-policy); alone, print it")
    p.add_argument("tenant"); p.add_argument("region", nargs="?", choices=DATA_REGIONS)
    args = ap.parse_args()
    if args.cmd == "add":
        add_member(args.tenant, args.email)
        print(f"{args.email.lower()} is on {args.tenant}")
    elif args.cmd == "policy":
        if args.region:
            print(f"{args.tenant}: data_region={set_policy(args.tenant, args.region)}")
        else:
            print(f"{args.tenant}: data_region={policy_for(args.tenant)}")
    else:
        for m in list_members(args.tenant):
            print(m)
