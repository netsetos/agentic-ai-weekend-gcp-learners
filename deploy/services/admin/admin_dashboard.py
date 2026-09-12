import os, pandas as pd, plotly.express as px, streamlit as st
from google.cloud import bigquery, firestore

_bq = bigquery.Client()
import google.auth
_fs = firestore.Client(project=(os.environ.get("GOOGLE_CLOUD_PROJECT") or google.auth.default()[1]))
DATASET = "documind_observability"

@st.cache_data(ttl=300)
def tenant_daily(tenant: str | None, days: int = 30) -> pd.DataFrame:
    where = "day >= DATE_SUB(CURRENT_DATE('Asia/Kolkata'), INTERVAL @days DAY)"
    params = [bigquery.ScalarQueryParameter("days", "INT64", days)]
    if tenant:
        where += " AND tenant = @t"
        params.append(bigquery.ScalarQueryParameter("t", "STRING", tenant))
    sql = f"SELECT * FROM `{DATASET}.tenant_daily` WHERE {where} ORDER BY day"
    return _bq.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).to_dataframe()

def usage_tab():
    st.subheader("Usage (last 30 days)")
    tenant = st.selectbox("Tenant filter", ["All"] + list_tenants()) or "All"
    df = tenant_daily(None if tenant == "All" else tenant)
    if df.empty: st.info("No queries in window."); return
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total queries", f"{df['queries'].sum():,}")
    col2.metric("Tokens (M)", f"{(df['tokens_in'].sum()+df['tokens_out'].sum())/1e6:.2f}")
    col3.metric("p95 latency", f"{df['p95_ms'].median():.0f} ms")
    unans_pct = 100*df["unanswerable"].sum()/max(1, df["queries"].sum())
    col4.metric("Unanswerable", f"{unans_pct:.1f} %")
    st.plotly_chart(px.bar(df, x="day", y="queries", color="tenant",
                           title="Queries per day"))
    st.plotly_chart(px.line(df, x="day", y=["p50_ms","p95_ms","p99_ms"],
                            title="Latency percentiles (ms)"))

def tenants_tab():
    st.subheader("Tenants")
    rows = []
    for t in _fs.collection("tenants").stream():
        d = t.to_dict(); d["id"] = t.id; rows.append(d)
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True)
    with st.expander("Create tenant"):
        tid = st.text_input("Tenant ID")
        tier = st.selectbox("Tier", ["free","pro","enterprise"])
        quota = st.number_input("Monthly budget (USD)", 10, 10000, 50)
        if st.button("Create") and tid:
            _fs.collection("tenants").document(tid).set({
                "tier": tier, "max_budget_usd": quota,
                "created_at": firestore.SERVER_TIMESTAMP,
            })
            from audit import emit
            emit("tenant.create", st.session_state.user,
                 {"type":"tenant","id":tid}, {"tier":tier,"budget":quota})
            st.success(f"Created {tid}"); st.rerun()

def audit_tab():
    st.subheader("Audit log (last 14 days)")
    action = st.selectbox("Action", ["all","user.login","doc.upload","doc.delete",
                                     "admin.rotate_key","tenant.suspend"])
    q = _fs.collection("audit_index").order_by("ts", direction="DESCENDING").limit(500)
    if action != "all": q = q.where("action", "==", action)
    rows = [d.to_dict() for d in q.stream()]
    st.dataframe(pd.DataFrame(rows), use_container_width=True)
    st.caption("Full 5-year audit is in GCS. Firestore index is hot 14 days only.")

def dlp_tab():
    st.subheader("DLP findings")
    q = _fs.collection("dlp_findings").order_by("scanned_at", direction="DESCENDING").limit(200)
    rows = [d.to_dict() for d in q.stream()]
    df = pd.DataFrame(rows)
    if df.empty: st.info("No findings."); return
    st.metric("Chunks with findings", len(df))
    flat = []
    for _, r in df.iterrows():
        for f in r["findings"]:
            flat.append({"tenant": r["tenant_id"], "type": f["info_type"],
                         "likelihood": f["likelihood"]})
    st.plotly_chart(px.histogram(pd.DataFrame(flat), x="type", color="likelihood",
                                 title="PII findings by type"))

def ingestion_tab():
    """The ledger, as the admin sees it (12 September 2026, deploy/INDEXING.md): every source's current version
    and what its last reindex cost - chunks reused by hash against chunks embedded - the rows retired, the date
    a document declares, and each tenant's corpus fingerprint (what the API's cache is keyed to). Read from
    sources/ and ledger/, which the ingest worker writes on every event; the drift the nightly reconcile measures
    is a log-based metric (alerts.tf) and pages on its own."""
    st.subheader("Ingestion - the ledger")
    rows = []
    for s in _fs.collection("sources").stream():
        d = s.to_dict() or {}
        at = d.get("indexed_at")
        rows.append({"tenant": d.get("tenant_id"), "document": (d.get("name") or "").split("/", 1)[-1],
                     "status": d.get("status"), "chunks": d.get("chunks"), "reused": d.get("reused"),
                     "embedded": d.get("embedded"), "retired": d.get("retired"),
                     "effective_from": d.get("effective_from") or "",
                     "embedding": f"{d.get('embedding_model') or '?'}@{d.get('embedding_version') or '?'}",
                     "indexed": at.strftime("%Y-%m-%d %H:%M") if hasattr(at, "strftime") else ""})
    if not rows:
        st.info("No sources yet: make ingest-corpus, or make backfill-current on a lane older than the ledger.")
        return
    df = pd.DataFrame(rows).sort_values(["tenant", "document"])
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Current versions", int((df["status"] == "indexed").sum()))
    col2.metric("Retired sources", int((df["status"] == "retired").sum()))
    col3.metric("Chunks reused (last reindexes)", int(df["reused"].fillna(0).sum()))
    col4.metric("Chunks embedded (last reindexes)", int(df["embedded"].fillna(0).sum()))
    st.dataframe(df, use_container_width=True)
    for l in _fs.collection("ledger").stream():
        d = l.to_dict() or {}
        st.caption(f"{l.id}: corpus fingerprint {d.get('fingerprint')} over {d.get('versions')} current versions "
                   f"(last event {d.get('last_event')})")
    recent = df[df["reused"].notna()]
    if not recent.empty:
        st.plotly_chart(px.bar(recent, x="document", y=["reused", "embedded"], color="tenant", barmode="group",
                               title="What the last reindex of each source cost: reused by hash vs embedded"))


def list_tenants() -> list[str]:
    return sorted(t.id for t in _fs.collection("tenants").stream())

def admin_page(user):
    st.title("🛠 DocuMind Admin")
    tabs = st.tabs(["Usage", "Tenants", "Audit Log", "DLP",
                    "Ingestion", "Graph", "Cost", "Quality",
                    "Context & Memory"])
    with tabs[0]: usage_tab()
    with tabs[1]: tenants_tab()
    with tabs[2]: audit_tab()
    with tabs[3]: dlp_tab()
    with tabs[4]: ingestion_tab()
    # Stubs, deliberately visible. An empty tab that says which lesson fills it
    # is a roadmap; a tab that is missing entirely is a surprise in the demo.
    for i, (name, owner) in enumerate([
            ("Graph", "4.6 - entity/edge counts and orphaned nodes"),
            ("Cost", "12.6 - INR per tenant per day, from tenant_daily"),
            ("Quality", "10.4 - golden-set scores per prompt_version"),
            ("Context & Memory", "8.6 - store size and recall hit rate")], start=5):
        with tabs[i]:
            st.info(f"**{name}** lands in lesson {owner}.")
