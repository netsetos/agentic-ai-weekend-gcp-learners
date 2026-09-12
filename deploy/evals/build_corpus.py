# -*- coding: utf-8 -*-
"""Build DocuMind's synthetic demo corpus.

Flagship Plan, Phase 0 (P0, Mon 7 Sept):
    "Synthetic corpus: ACME/Zeta/Globex contracts, Hindi-English invoice with fake PAN,
     chart annual report, 6-min MP4, whiteboard photo, 40k-token policy pack;
     golden.jsonl v0 (30 rows + tenant-isolation)"
    Why: "Every demo needs the same data; no real PII on screen."

EVERYTHING HERE IS SYNTHETIC. The PAN, GSTIN, Aadhaar and mobile numbers are
format-valid so the DLP and Model Armor demos actually fire on them, and they are
invented so nothing real is ever on a shared screen. Regenerating is deterministic:
same input, same bytes, so a diff means somebody changed the corpus on purpose.

    python deploy/evals/build_corpus.py          # writes corpus/ + manifest.json
    bash   deploy/evals/upload.sh                # pushes to gs://<project>-uploads

The clause ids (EXP-12, LV-01, PB-02, NP-03, LV-07, PR-05, IT-SEC-04) are the ones
lesson 4.7's golden rows already reference, so 4.7's ten rows run against this corpus
unchanged and golden.jsonl v0 is a superset of them.
"""
import hashlib
import json
import os
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, "corpus")

# --------------------------------------------------------------------- synthetic identifiers
# Format-valid, entirely invented. PAN: 5 alpha, 4 digit, 1 alpha.
FAKE = {
    "pan": "AAAPZ1234C",
    "gstin": "27AAAPZ1234C1ZV",
    "mobile": "+919876543210",
    "aadhaar": "2234 5678 9012",
    "email": "accounts@acme-demo.invalid",
}

TENANTS = ["acme", "zeta", "globex"]

# ------------------------------------------------------------------------- the clause bank
# (clause_id, heading, body). These carry the answers the golden rows assert on.
CLAUSES = [
    ("NP-03", "Notice period",
     "A confirmed employee at grade {grade} or above serves a notice period of "
     "{notice} days. "
     "Notice runs from the date the resignation is acknowledged in writing. "
     "Unused earned leave may not be set off against the notice period."),
    ("PB-02", "Probation",
     "New joiners serve six months on probation at grade E2. During probation the "
     "notice period is {probation} days for either side. Probation may be extended once, by "
     "up to three months, with written reasons."),
    ("LV-01", "Earned leave",
     "Earned leave accrues at 1.75 days per completed month. A maximum of {carry} days "
     "may be carried forward into the next calendar year; anything above {carry} lapses "
     "on 31 December."),
    ("LV-07", "Leave on exit",
     "Earned leave is encashed on exit at basic pay, capped at {encash} days. Leave "
     "cannot be encashed during probation and cannot be used to shorten notice."),
    ("EXP-12", "Travel reimbursement",
     "Domestic travel is reimbursed against original receipts, capped at Rs {cap} "
     "per trip. Anything above the cap needs written approval from the function "
     "head before travel, not after."),
    ("PR-05", "Payroll and Form 16",
     "Salary is credited on the last working day of each month. Form 16 is issued "
     "by 15 June for the preceding financial year."),
    ("IT-SEC-04", "Removable media",
     "USB mass-storage devices are blocked on all company laptops. No exception is "
     "granted for contractors. Data transfer uses the approved cloud bucket only."),
    ("SEC-09", "Access review",
     "Production access is reviewed quarterly. Any account unused for 45 days is "
     "disabled automatically and must be re-approved to restore."),
    ("FIN-02", "Purchase approval",
     "Purchases up to Rs 2,00,000 are approved by the function head. Above that, "
     "the CFO approves. Splitting a purchase to stay under a threshold is a "
     "disciplinary matter."),
    ("WFH-01", "Remote work",
     "Employees may work remotely up to eight days per month with manager consent. "
     "Remote work from outside India requires prior tax clearance."),
]

FILLER_TOPICS = [
    "Attendance and shift rosters", "Grievance redressal", "Anti-harassment",
    "Health insurance and dependants", "Relocation assistance", "Referral bonus",
    "Learning reimbursement", "Asset custody", "Business conduct",
    "Conflict of interest", "Data classification", "Incident reporting",
    "Vendor onboarding", "Statutory holidays", "Maternity and adoption",
    "Retirement benefits", "Performance review cycle", "Internal transfer",
]


# The figures MUST differ per tenant. A corpus where every tenant's handbook says
# Rs 40,000 cannot demonstrate tenant isolation: the "leaked" answer is also the
# correct one, so the test passes whether or not the filter works.
FIGURES = {
    "acme":  {"cap": "40,000", "notice": "60", "carry": "30", "encash": "45",
              "grade": "E3", "probation": "15"},
    "zeta":  {"cap": "25,000", "notice": "30", "carry": "18", "encash": "20",
              "grade": "L4", "probation": "7"},
    "globex": {"cap": "60,000", "notice": "45", "carry": "24", "encash": "36",
               "grade": "B2", "probation": "21"},
}


def clauses_for(tenant: str):
    """The clause bank with this tenant's numbers substituted in."""
    f = FIGURES[tenant]
    out = []
    for cid, heading, body in CLAUSES:
        b = (body.replace("{cap}", f["cap"]).replace("{notice}", f["notice"])
                 .replace("{carry}", f["carry"]).replace("{encash}", f["encash"])
                 .replace("{grade}", f["grade"]).replace("{probation}", f["probation"]))
        out.append((cid, heading, b))
    return out


def policy_pack(tenant: str, target_tokens: int = 40_000) -> str:
    """A policy handbook long enough to force real context work.

    ~4 chars per token is the working estimate the course uses, so 40k tokens is
    roughly 160k characters. Deterministic: the filler is generated from a counter,
    not a random source, so regenerating produces identical bytes.
    """
    out = [f"# {tenant.upper()} Employee Handbook 2026",
           "",
           "Synthetic document. Every name, number and identifier is invented.",
           ""]
    for cid, heading, body in clauses_for(tenant):
        out += [f"## {cid} — {heading}", "", textwrap.fill(body, 88), ""]

    n = 0
    target_chars = target_tokens * 4
    while sum(len(x) + 1 for x in out) < target_chars:
        topic = FILLER_TOPICS[n % len(FILLER_TOPICS)]
        sec = f"GEN-{n:03d}"
        para = (
            f"This section restates {tenant.upper()}'s standing position on "
            f"{topic.lower()}. It applies to all employees and to contractors "
            f"working on {tenant.upper()} premises or systems. Where this section "
            f"conflicts with a signed statement of work, the statement of work "
            f"governs for that engagement only and this handbook governs "
            f"everywhere else. Questions go to the People team, who will answer "
            f"within five working days. Nothing in this section creates an "
            f"entitlement beyond what the employment contract provides. "
            f"Reference {sec} when raising a query so it routes correctly."
        )
        out += [f"## {sec} — {topic}", "", textwrap.fill(para, 88), ""]
        n += 1
    return "\n".join(out)


def msa(tenant: str, notice_days: int, cap_lakhs: int, sla_pct: str) -> str:
    T = tenant.upper()
    return "\n".join([
        f"# Master Services Agreement — {T} and DocuMind Technologies",
        "",
        "Synthetic contract. Not legal advice and not a real agreement.",
        "",
        "## MSA-01 — Term",
        "",
        "This agreement runs for 24 months from the effective date and renews for",
        "successive 12-month terms unless either party gives notice.",
        "",
        "## MSA-04 — Termination",
        "",
        f"Either party may terminate for convenience on {notice_days} days written",
        "notice. Termination for cause is immediate on written notice where a",
        "material breach is not cured within 30 days.",
        "",
        "## MSA-07 — Liability cap",
        "",
        f"Aggregate liability is capped at Rs {cap_lakhs},00,000 or the fees paid in",
        "the preceding 12 months, whichever is lower.",
        "",
        "## MSA-09 — Service levels",
        "",
        f"The service target is {sla_pct} monthly availability, measured on",
        "successful responses to /v1/query. Service credits are the sole remedy.",
        "",
        "## MSA-12 — Data residency",
        "",
        f"{T} data is processed and stored in India (asia-south1). Sub-processors",
        "outside India require prior written consent.",
    ])


def invoice_hinglish() -> str:
    """A Hindi-English invoice carrying synthetic PAN / GSTIN / mobile.

    Code-mixed on purpose: it is the register Indian back-office documents are
    actually written in, and it is what a guard tuned on English alone misses.
    """
    return "\n".join([
        "# INVOICE / चालान",
        "",
        "Synthetic document. PAN, GSTIN, mobile and Aadhaar below are invented.",
        "",
        "Invoice No: INV-2026-0412",
        "Date / दिनांक: 12 April 2026",
        "",
        "Bill To / प्राप्तकर्ता: ACME Manufacturing Pvt Ltd",
        "Address: Plot 14, HITEC City, Hyderabad 500081",
        f"PAN: {FAKE['pan']}",
        f"GSTIN: {FAKE['gstin']}",
        f"Contact / संपर्क: {FAKE['mobile']}",
        f"Email: {FAKE['email']}",
        "",
        "| Description / विवरण | Qty | Rate (Rs) | Amount (Rs) |",
        "|---|---|---|---|",
        "| Document processing — standard / मानक | 12,000 | 10.00 | 1,20,000 |",
        "| Priority queue / प्राथमिकता | 3,000 | 12.00 | 36,000 |",
        "",
        "Subtotal / उप-योग: Rs 1,56,000",
        "GST @ 18% / जीएसटी: Rs 28,080",
        "**Total payable / कुल देय: Rs 1,84,500**",
        "",
        "Payment terms / भुगतान शर्तें: 30 days from invoice date.",
        "Late payment attracts 1.5% per month / विलंब शुल्क 1.5% प्रति माह.",
    ])


def annual_report() -> str:
    """Chart-bearing report. The figures live in the table so a text-only
    pipeline can answer, and 9.x can later ask the same questions of the chart."""
    return "\n".join([
        "# ACME Manufacturing — Annual Report FY2026",
        "",
        "Synthetic financials. Invented figures.",
        "",
        "## AR-02 — Revenue by region",
        "",
        "| Region | FY2025 (Rs cr) | FY2026 (Rs cr) | Growth |",
        "|---|---|---|---|",
        "| India | 412 | 508 | 23.3% |",
        "| APAC ex-India | 188 | 236 | 25.5% |",
        "| EMEA | 96 | 91 | -5.2% |",
        "| Americas | 143 | 170 | 18.9% |",
        "| **Total** | **839** | **1,005** | **19.8%** |",
        "",
        "[Figure 3, p.12] Revenue by region, FY2025 vs FY2026 — grouped bar chart.",
        "",
        "## AR-05 — Headcount",
        "",
        "Headcount closed at 4,180, up from 3,742. Attrition was 11.4%, down from",
        "14.9% in FY2025.",
        "",
        "## AR-09 — Capital expenditure",
        "",
        "Capex was Rs 78 crore, of which Rs 31 crore was the Hyderabad plant",
        "expansion and Rs 12 crore was cloud and data platform.",
    ])


def townhall_script() -> str:
    """The FY2026 town hall, as a transcript. Two speakers, about three minutes read aloud.

    It is the SCRIPT of the synthetic video `make media --video` synthesises (two Chirp 3 HD
    voices over four slides), and it is committed as text so the golden set can be verified
    against it offline the way a real Act is verified against its pypdf mirror. upload.sh keeps
    it home when the .mp4 twin exists: the corpus then holds the VIDEO, and 9.4's segments and
    9.6's segment citations come from what the worker heard, not from this file. Every figure is
    the annual report's own, so a segment and the report's text agree.
    """
    return "\n".join([
        "# ACME Manufacturing — FY2026 Town Hall (transcript)",
        "",
        "Synthetic. Two synthetic speakers reading synthetic financials; no real person's voice.",
        "The video in the corpus, townhall_2026_q1.mp4, is synthesised from this script.",
        "",
        "Meera (CEO): Good morning, everyone, and welcome to the FY2026 town hall. Thank you for",
        "joining from Hyderabad, Pune and the regional offices. We closed the year at one thousand",
        "and five crore rupees of revenue, up 19.8 per cent on FY2025. That is the headline, and",
        "Arjun will take you through the regions in a moment.",
        "",
        "Arjun (CFO): Thanks, Meera. Let me start with the table you all have on slide two.",
        "India grew from 412 to 508 crore, up 23.3 per cent. APAC excluding India grew from 188 to",
        "236 crore, up 25.5 per cent, our fastest region. The Americas grew from 143 to 170 crore,",
        "up 18.9 per cent. EMEA is the one region that shrank: revenue fell 5.2 per cent, from 96",
        "crore to 91 crore. Two large renewals in Germany slipped into the first quarter of FY2027,",
        "and we expect EMEA to recover once they close.",
        "",
        "Meera (CEO): On people, headcount closed at 4,180, up from 3,742, and attrition came down",
        "to 11.4 per cent from 14.9 per cent. That improvement is the one I am proudest of.",
        "",
        "Arjun (CFO): On capital expenditure we invested 78 crore during the year. 31 crore went",
        "into the Hyderabad plant expansion and 12 crore into cloud and the data platform, which",
        "is where DocuMind runs. Every figure I have quoted is in the annual report, section AR-02",
        "for revenue, AR-05 for headcount and AR-09 for capex.",
        "",
        "Meera (CEO): Thank you, Arjun. Questions are open on the portal until Friday, and the",
        "recording and this transcript will be in DocuMind by this afternoon. Thank you all.",
    ])


DOCS = {
    "acme": [
        ("hr_policy_2026.md", lambda: policy_pack("acme"), "policy"),
        ("msa_acme_2026.md", lambda: msa("acme", 90, 50, "99.5%"), "contract"),
        ("inv_2026_0412.md", invoice_hinglish, "invoice"),
        ("annual_report_2026.md", annual_report, "report"),
        ("townhall_2026_q1.md", townhall_script, "transcript"),
    ],
    "zeta": [
        ("hr_policy_zeta_2026.md", lambda: policy_pack("zeta"), "policy"),
        ("msa_zeta_2026.md", lambda: msa("zeta", 30, 25, "99.9%"), "contract"),
    ],
    "globex": [
        ("msa_globex_2026.md", lambda: msa("globex", 120, 100, "99.0%"), "contract"),
    ],
}

# Binary assets the demos need, in two lists. RENDERED is what `make media` (evals/build_media.py,
# lesson 9.x) draws from the text documents above and from one real Act page - deterministic,
# committed beside the text. BINARY is what text cannot make: the town hall VIDEO, which
# `make media --video` synthesises from townhall_2026_q1.md with two Chirp 3 HD voices and
# ffmpeg (gitignored; or record your own), and a photograph the owner supplies.
RENDERED = [
    ("acme", "annual_report_2026_fig3.png", "figure",
     "Figure 3 of the annual report, drawn from its own AR-02 table (matplotlib) - the figure "
     "the text has referenced since Module 4 and never had."),
    ("acme", "inv_2026_0412.png", "figure",
     "The Hinglish invoice rendered as an image (English half of each label), so 9.1 and 9.3 "
     "OCR a document whose ground truth (lk-10, jn-05) is in the same corpus."),
    ("acme", "payment_of_bonus_act_1965_p30.png", "figure",
     "Page 30 of the real Payment of Bonus Act 1965 - the Fourth Schedule's set-on / set-off "
     "table - rendered by pypdfium2. A real table on a real page."),
]
BINARY = [
    ("acme", "townhall_2026_q1.mp4", "video",
     "About three minutes: two synthetic voices reading townhall_2026_q1.md over four slides. "
     "`make media --video` builds it (the Text-to-Speech API and ffmpeg: apt-get it in Cloud Shell, or "
     "pip install imageio-ffmpeg). Or "
     "record a real product walkthrough of the same length - no real faces or voices."),
    ("acme", "whiteboard_arch.png", "image",
     "Photograph of a hand-drawn architecture sketch, for the 9.x image-caption "
     "path. Draw it, photograph it, check no colleague is in frame."),
]


def main():
    os.makedirs(CORPUS, exist_ok=True)
    manifest = []
    for tenant, docs in DOCS.items():
        d = os.path.join(CORPUS, tenant)
        os.makedirs(d, exist_ok=True)
        for name, fn, doc_type in docs:
            body = fn()
            p = os.path.join(d, name)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(body)
            sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
            manifest.append({
                "tenant_id": tenant, "slug": name.rsplit(".", 1)[0],
                "file": f"corpus/{tenant}/{name}", "doc_type": doc_type,
                "gcs_uri": f"gs://${{PROJECT_ID}}-uploads/{tenant}/{name}",
                "sha256": sha, "chars": len(body),
                "approx_tokens": round(len(body) / 4),
            })
    for supplied_by, assets in (("make media", RENDERED), ("owner", BINARY)):
        for tenant, name, doc_type, note in assets:
            manifest.append({
                "tenant_id": tenant, "slug": name.rsplit(".", 1)[0],
                "file": f"corpus/{tenant}/{name}", "doc_type": doc_type,
                "gcs_uri": f"gs://${{PROJECT_ID}}-uploads/{tenant}/{name}",
                "sha256": None, "chars": None, "approx_tokens": None,
                "supplied_by": supplied_by, "note": note,
            })
    real = real_entries()
    manifest += real
    with open(os.path.join(HERE, "manifest.json"), "w", encoding="utf-8",
              newline="\n") as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False)
        f.write("\n")

    print(f"{'tenant':8} {'slug':36} {'type':9} {'tokens':>8}  sha256")
    print("-" * 90)
    for m in manifest:
        tok = f"{m['approx_tokens']:,}" if m["approx_tokens"] else ("scan" if m.get("real") else "owner")
        sha = (m["sha256"] or "")[:12] or "-"
        print(f"{m['tenant_id']:8} {m['slug'][:36]:36} {m['doc_type']:9} {tok:>8}  {sha}"
              f"{'  REAL' if m.get('real') else ''}")
    text = [m for m in manifest if m["approx_tokens"]]
    print("-" * 90)
    print(f"{len(text)} text documents across {len(DOCS)} tenants, "
          f"{sum(m['approx_tokens'] for m in text):,} tokens total; "
          f"{len(real)} of them real (evals/real_sources.json, fetched by fetch_real.py)")
    print(f"{len(RENDERED)} media assets `make media` renders, "
          f"{len(BINARY)} binary assets the owner supplies (see manifest notes)")
    print("\nNo real PII: PAN/GSTIN/Aadhaar/mobile above are format-valid and invented; the real"
          "\ndocuments are Acts of Parliament, which carry none.")
    missing = [m for m in real if not os.path.isfile(os.path.join(HERE, m["file"]))]
    if missing:
        print(f"\n{len(missing)} real document(s) not on disk yet - run: python {HERE}/fetch_real.py")


# ------------------------------------------------------------------- the real documents
# Thirteen real documents, fetched by fetch_real.py from the ministry's own site and listed
# with their sha256 in real_sources.json. This function never touches the network: it turns
# that list, plus whatever fetch_real.py has already written under corpus/, into manifest
# rows of the same shape as the synthetic ones. The PDF is the object upload.sh pushes (the
# ingest worker and lesson 4.1 parse it with Document AI); the .md beside it is the pypdf
# mirror that the offline gate and the notebooks' zero-cost seed path read. A scanned Act
# has no mirror, and therefore no chars: only the OCR lane can read it.
def real_entries():
    path = os.path.join(HERE, "real_sources.json")
    if not os.path.isfile(path):
        return []
    out = []
    for src in json.load(open(path, encoding="utf-8")):
        for tenant in src["tenants"]:
            mirror = os.path.join(CORPUS, tenant, src["slug"] + ".md")
            chars = len(open(mirror, encoding="utf-8").read()) if os.path.isfile(mirror) else None
            out.append({
                "tenant_id": tenant, "slug": src["slug"],
                "file": f"corpus/{tenant}/{src['slug']}.pdf", "doc_type": src["doc_type"],
                "gcs_uri": f"gs://${{PROJECT_ID}}-uploads/{tenant}/{src['slug']}.pdf",
                "sha256": src["sha256"], "chars": chars,
                "approx_tokens": round(chars / 4) if chars else None,
                "real": True, "pages": src["pages"], "text_layer": src["text_layer"],
                "title": src["title"], "source_url": src["source_url"],
                "publisher": src["publisher"], "retrieved": src["retrieved"],
                "supplied_by": "fetch_real.py", "note": src["note"],
            })
    return out


if __name__ == "__main__":
    main()
