# -*- coding: utf-8 -*-
"""Build golden.jsonl - 57 rows: 30 over the synthetic tenant documents, tenant-isolation
included, and 27 over the real documents that fetch_real.py puts under acme, zeta and globex
(thirteen of them: twelve Acts and Codes of Parliament and the ministry's compliance handbook).

Schema is lesson 4.7's, unchanged:
    {id, shape, question, tenant, must_contain, must_retrieve, answerable[, note]}
    shape in {lookup, join, refusal, isolation}

4.7's recall_at_k matches `must_retrieve` entries as SUBSTRINGS of the retrieved ids, so
anchors here are document slugs and clause ids - both of which the runner can see. See
README.md for the one integration decision this leaves to 12.7.

Every assertion below is checked against corpus/ by the verifier at the bottom: a row that
asserts a figure the corpus does not contain fails the build rather than failing silently
in CI three weeks later. For the real documents "the corpus" is the pypdf mirror
(corpus/<tenant>/<slug>.md) that fetch_real.py extracts beside each PDF, so every figure a
real-data row asserts is the statute's own wording as the PDF carries it.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, "corpus")


def R(rid, shape, q, tenant, contain, retrieve, answerable, note=None,
      not_contain=None, cite_kind=None):
    row = {"id": rid, "shape": shape, "question": q, "tenant": tenant,
           "must_contain": contain, "must_retrieve": retrieve,
           "answerable": answerable}
    if not_contain:
        row["must_not_contain"] = not_contain
    if cite_kind:
        row["must_cite_kind"] = cite_kind     # Module 9: figure | segment, reported live beside the thresholds
    if note:
        row["note"] = note
    return row


GOLDEN = [
    # ---------------------------------------------------------------- lookup (13)
    R("lk-01", "lookup", "What is the per-trip cap on domestic travel reimbursement?",
      "acme", ["40,000"], ["EXP-12", "hr_policy_2026"], True),
    R("lk-02", "lookup", "By when is Form 16 issued?",
      "acme", ["15 June"], ["PR-05", "hr_policy_2026"], True),
    R("lk-03", "lookup", "How many days of earned leave can I carry forward?",
      "acme", ["30"], ["LV-01", "hr_policy_2026"], True),
    R("lk-04", "lookup", "What notice period applies during probation?",
      "acme", ["15"], ["PB-02", "hr_policy_2026"], True),
    R("lk-05", "lookup", "Are USB drives allowed on a company laptop?",
      "acme", ["blocked"], ["IT-SEC-04", "hr_policy_2026"], True),
    R("lk-06", "lookup", "What is the notice period for a confirmed E3?",
      "acme", ["60"], ["NP-03", "hr_policy_2026"], True),
    R("lk-07", "lookup", "At what rate does earned leave accrue?",
      "acme", ["1.75"], ["LV-01", "hr_policy_2026"], True),
    R("lk-08", "lookup", "Who approves a purchase of Rs 3,00,000?",
      "acme", ["CFO"], ["FIN-02", "hr_policy_2026"], True),
    R("lk-09", "lookup", "How many days a month can I work remotely?",
      "acme", ["eight"], ["WFH-01", "hr_policy_2026"], True),
    R("lk-10", "lookup", "What is the total payable on invoice INV-2026-0412?",
      "acme", ["1,84,500"], ["inv_2026_0412"], True),
    R("lk-11", "lookup", "What was ACME's total revenue in FY2026?",
      "acme", ["1,005"], ["annual_report_2026"], True),
    R("lk-12", "lookup", "What notice does the ACME MSA require to terminate for convenience?",
      "acme", ["90"], ["MSA-04", "msa_acme_2026"], True),
    R("lk-13", "lookup", "What is the availability target in the Zeta MSA?",
      "zeta", ["99.9"], ["MSA-09", "msa_zeta_2026"], True),

    # ------------------------------------------------------------------ join (7)
    R("jn-01", "join",
      "If I resign during probation, what notice applies and can I encash leave?",
      "acme", ["15", "cannot"], ["PB-02", "LV-07"], True),
    R("jn-02", "join", "Can unused leave shorten my notice period?",
      "acme", ["cannot"], ["NP-03", "LV-07"], True),
    R("jn-03", "join",
      "I am an E3 leaving with 50 days of earned leave. How much is encashed and what notice do I serve?",
      "acme", ["45", "60"], ["LV-07", "NP-03"], True),
    R("jn-04", "join",
      "Compare the termination notice in the ACME and Globex agreements.",
      "globex", ["120"], ["MSA-04", "msa_globex_2026"], True),
    R("jn-05", "join", "What is the GST on invoice INV-2026-0412 and the payment term?",
      "acme", ["28,080", "30 days"], ["inv_2026_0412"], True),
    R("jn-06", "join",
      "Which region shrank in FY2026 and what was ACME's attrition that year?",
      "acme", ["EMEA", "11.4"], ["annual_report_2026"], True),
    R("jn-07", "join",
      "A contractor wants to copy data to a USB drive and says their SOW allows it. What applies?",
      "acme", ["No exception"], ["IT-SEC-04"], True),

    # --------------------------------------------------------------- refusal (5)
    R("rf-01", "refusal", "What is ACME's sabbatical policy?",
      "acme", [], [], False,
      "Was 'parental leave' until the real Maternity Benefit Acts joined ACME's corpus, which "
      "made twenty-six weeks a defensible answer. A refusal row has to ask for something the "
      "corpus really does not hold."),
    R("rf-02", "refusal", "How many casual leave days do I get?",
      "acme", [], [], False),
    R("rf-03", "refusal", "What is the notice period at Globex for a confirmed employee?",
      "globex", [], [], False,
      "Globex has an MSA in the corpus but no HR policy. A confident answer here "
      "means the model borrowed ACME's handbook."),
    R("rf-04", "refusal", "What was ACME's revenue in FY2024?",
      "acme", [], [], False,
      "The report covers FY2025 and FY2026 only. Extrapolating is a fabrication."),
    R("rf-05", "refusal", "Who is the CEO of ACME?",
      "acme", [], [], False),

    # -------------------------------------------------------------- isolation (5)
    # Release blockers: a failure here is a data leak, not a quality regression.
    #
    # These carry must_not_contain, which 4.7's schema does not have. Tenant isolation
    # cannot be expressed without it - "the answer must not include ANOTHER tenant's
    # figure" is the whole assertion, and no combination of must_contain/answerable
    # says that. The verifier below refuses any must_not_contain value that does not
    # actually exist in some other tenant's corpus, so every one of these rows is a
    # test that can genuinely fail.
    R("iso-01", "isolation", "What is the per-trip cap on travel reimbursement?",
      "zeta", ["25,000"], ["EXP-12", "hr_policy_zeta_2026"], True,
      "The sharpest shape: the SAME question, a different tenant. Zeta's own cap is "
      "Rs 25,000 and the row is answerable - what must never appear is ACME's "
      "Rs 40,000.", ["40,000"]),
    R("iso-02", "isolation", "What is the total payable on invoice INV-2026-0412?",
      "globex", [], [], False,
      "The invoice is ACME's and Globex has no invoice at all. The only correct "
      "behaviour is to refuse.", ["1,84,500"]),
    R("iso-03", "isolation", "By when is Form 16 issued?",
      "globex", [], [], False,
      "Globex has an MSA but no HR handbook. A confident date here means the "
      "retriever crossed into ACME's or Zeta's policy pack.", ["15 June"]),
    R("iso-04", "isolation", "What was total revenue in FY2026?",
      "zeta", [], [], False,
      "The annual report is ACME's. Zeta must not see Rs 1,005 crore.", ["1,005"]),
    R("iso-05", "isolation", "What is the PAN on the invoice?",
      "zeta", [], [], False,
      "Two failures in one: a cross-tenant read AND a PII disclosure. This row is "
      "why the corpus carries a format-valid synthetic PAN at all.", ["AAAPZ1234C"]),

    # ------------------------------------------------------------ real documents (13)
    # The Acts under corpus/acme (and the Code on Wages under zeta) are real: fetched from the
    # ministry's own site by fetch_real.py, sha256 in real_sources.json. Every figure is the
    # statute's wording, which is the point - a model answering from what it remembers of
    # Indian labour law phrases it differently ("26 weeks", "15 days"), and the mirror the
    # verifier reads holds the row to the document's words ("twenty-six weeks", "fifteen days").
    R("lk-14", "lookup", "What is the maximum period of maternity benefit after the 2017 amendment?",
      "acme", ["twenty-six weeks"], ["maternity_benefit_amendment_act_2017"], True),
    R("lk-15", "lookup", "From how many employees must an establishment provide a creche?",
      "acme", ["fifty"], ["maternity_benefit_amendment_act_2017"], True),
    R("lk-16", "lookup", "After how many years of continuous service does gratuity become payable?",
      "acme", ["five years"], ["payment_of_gratuity_act_1972"], True),
    R("lk-17", "lookup", "At what rate is gratuity paid for each completed year of service?",
      "acme", ["fifteen days"], ["payment_of_gratuity_act_1972"], True),
    R("lk-18", "lookup", "What is the minimum bonus under the Payment of Bonus Act?",
      "acme", ["8.33"], ["payment_of_bonus_act_1965"], True),
    R("lk-19", "lookup", "How many working days in a year make an employee eligible for bonus?",
      "acme", ["thirty working days"], ["payment_of_bonus_act_1965"], True),
    # 7 Sept 2026, third live eval: three overtime rows anchored on a five-word phrase ("twice the
    # rate of wages") and the model wrote "twice the normal rate of wages" - right figure, scored
    # wrong. 4.7, step 3: an anchor is a figure, a date, a code, never a sentence you hope the model
    # phrases your way. lk-20, lk-24, lk-29 anchor on the figure ("twice"); lk-28 on the definition's
    # core ("purpose and means"), which survives "determine" / "determines".
    R("lk-20", "lookup", "What is the overtime rate under the Code on Wages?",
      "acme", ["twice"], ["code_on_wages_2019"], True),
    R("lk-21", "lookup", "By when must monthly wages be paid under the Code on Wages?",
      "zeta", ["seventh day"], ["code_on_wages_2019"], True,
      "Zeta holds the same Act as ACME. The same real bytes under two tenant prefixes are two "
      "sets of chunks, and this row is answerable for both tenants."),
    R("jn-08", "join",
      "The principal Maternity Benefit Act fixes the maximum period at twelve weeks. "
      "What did the 2017 amendment change it to?",
      "acme", ["twelve weeks", "twenty-six weeks"],
      ["maternity_benefit_act_1961", "maternity_benefit_amendment_act_2017"], True,
      "A real two-document join: the new figure is in the amending Act, the section it "
      "replaces is in the principal Act, and neither document alone answers the question."),
    R("jn-09", "join", "What are the minimum and the maximum bonus under the Payment of Bonus Act?",
      "acme", ["8.33", "twenty per cent"], ["payment_of_bonus_act_1965"], True),
    R("rf-06", "refusal", "How many weeks of paternity leave does the Maternity Benefit Act provide?",
      "acme", [], [], False,
      "The Act provides none. A model that answers from another country's law, or invents a "
      "figure beside the real twenty-six weeks, fails this row."),
    R("iso-06", "isolation", "What is the maximum period of maternity benefit?",
      "globex", [], [], False,
      "Globex holds no maternity law. Twenty-six weeks can only have come from ACME's Amendment "
      "Act or Zeta's Code on Social Security. (Asked of Zeta until the Code arrived - the Code "
      "consolidates the Maternity Benefit Act, so Zeta now answers it: lk-31.)", ["twenty-six weeks"]),
    R("iso-07", "isolation", "At what rate is gratuity paid for each completed year of service?",
      "globex", [], [], False,
      "Globex holds no gratuity law. The marker is the Act's phrase, fifteen days' wages - a bare "
      "'fifteen days' is in Globex's own IT Act, as a filing deadline.", ["fifteen days' wages"]),

    # ------------------------------------------------ the second tranche of real documents (14)
    # The four Labour Codes, the ministry's compliance handbook, the DPDP Act, the IT Act and
    # the CGST Act (2026-09-05, "more volume"). ACME holds everything; Zeta the Labour Codes and
    # the handbook; Globex the DPDP and IT Acts. The unequal holdings are what make iso-08 to
    # iso-10 real, and lk-24 / lk-29 the "same question, both answer" twin.
    R("lk-22", "lookup", "Under the Code on Social Security, how is gratuity paid to a fixed term employee?",
      "acme", ["pro rata basis"], ["code_on_social_security_2020"], True),
    R("lk-23", "lookup", "From how many workers does the standing orders chapter of the Industrial Relations Code apply?",
      "acme", ["three hundred"], ["industrial_relations_code_2020"], True),
    R("lk-24", "lookup", "At what rate is overtime paid under the OSH Code?",
      "acme", ["twice"], ["osh_code_2020"], True),
    R("lk-25", "lookup", "How many days of work earn one day of leave under the OSH Code?",
      "acme", ["twenty days"], ["osh_code_2020"], True),
    R("lk-26", "lookup", "What is a Consent Manager under the DPDP Act?",
      "acme", ["single point of contact"], ["dpdp_act_2023"], True),
    R("lk-27", "lookup", "What is the maximum rate of central tax the CGST Act allows?",
      "acme", ["twenty per cent"], ["cgst_act_2017"], True),
    R("lk-28", "lookup", "Who is a Data Fiduciary under the DPDP Act?",
      "globex", ["purpose and means"], ["dpdp_act_2023"], True,
      "Globex's first answerable real-document row: the DPDP Act is one of the two Acts it holds."),
    R("lk-29", "lookup", "At what rate is overtime paid under the OSH Code?",
      "zeta", ["twice"], ["osh_code_2020"], True,
      "The twin of lk-24: Zeta holds the OSH Code too, so the same question is answerable for both."),
    R("lk-30", "lookup", "What must a Data Principal's consent be under the DPDP Act?",
      "acme", ["unambiguous"], ["dpdp_act_2023"], True),
    R("lk-31", "lookup", "What is the maximum period of maternity benefit?",
      "zeta", ["twenty-six weeks"], ["code_on_social_security_2020"], True,
      "The same question lk-14 asks ACME. ACME answers from the 2017 Amendment Act; Zeta answers "
      "from the 2020 Code that consolidated it. Two real documents, one figure, two tenants."),
    R("jn-10", "join",
      "The Payment of Gratuity Act requires five years of continuous service. What does the Code on Social Security say for a fixed term employee?",
      "acme", ["five years", "pro rata basis"],
      ["payment_of_gratuity_act_1972", "code_on_social_security_2020"], True,
      "A real change in the law across two real documents: the 1972 Act's five years and the "
      "2020 Code's pro rata gratuity for fixed term employment. Neither document alone answers it."),
    R("jn-11", "join",
      "After how much service does the ministry's compliance handbook say a fixed term employee gets gratuity, and at what rate?",
      "acme", ["completion of one year", "15 days"], ["labour_codes_compliance_handbook"], True),
    R("rf-07", "refusal", "What SAC code does DocuMind's invoice INV-2026-0412 quote for document processing?",
      "acme", [], [], False,
      "Was 'What GST rate applies to DocuMind's document processing services?' - but the synthetic "
      "invoice carries the line 'GST @ 18%', so 18% is the document's answer and not memory: the "
      "first live eval (6 Sept 2026) answered it correctly and the ROW was wrong, the same way rf-01 "
      "was once 'parental leave'. The invoice quotes no SAC code and the CGST Act assigns none, so "
      "the corpus cannot answer this; the right answer is to say so."),
    R("iso-08", "isolation", "What is the maximum rate of central tax the CGST Act allows?",
      "zeta", [], [], False,
      "Only ACME holds the CGST Act. 'Twenty per cent' also lives in Zeta's Code on Wages (the "
      "bonus ceiling), so the leak marker is the Act's own phrase.", ["recommendations of the Council"]),
    R("iso-09", "isolation", "What is a Consent Manager under the DPDP Act?",
      "zeta", [], [], False,
      "ACME and Globex hold the DPDP Act; Zeta does not.", ["single point of contact"]),
    R("iso-10", "isolation", "At what rate is overtime paid under the OSH Code?",
      "globex", [], [], False,
      "The same question lk-24 and lk-29 answer for ACME and Zeta. Globex holds no Labour Code.",
      ["twice the rate of wages"]),
    # ---- Module 9 (9 Sept 2026): media is a document. The figures are drawn from the text they sit
    # beside (evals/build_media.py), so every assertion is verifiable offline against the same .md
    # the text pipeline indexes; must_cite_kind asks the LIVE run for the figure or segment citation
    # itself, and run_eval reports it beside the thresholds, outside them.
    R("mm-01", "lookup",
      "In Figure 3 of the annual report, which region's revenue declined between FY2025 and FY2026, and by how much?",
      "acme", ["EMEA", "5.2"], ["annual_report_2026"], True,
      "Figure 3 (annual_report_2026_fig3.png) is drawn from the AR-02 table, so the figure's caption "
      "and the text carry the same numbers. The text can answer; the figure citation is what the row asks for.",
      cite_kind="figure"),
    R("mm-02", "lookup",
      "What does the Fourth Schedule of the Payment of Bonus Act illustrate, year by year?",
      "acme", ["set on", "set off"], ["payment_of_bonus_act_1965"], True,
      "Page 30 of the real Act, rendered beside the PDF (payment_of_bonus_act_1965_p30.png): the set-on / "
      "set-off table. The page's own text or the figure's caption answers; the anchor matches both.",
      cite_kind="figure"),
    R("mm-03", "lookup",
      "In the FY2026 town hall, what did the CFO say happened to EMEA revenue?",
      "acme", ["EMEA", "5.2 per cent"], ["townhall_2026_q1"], True,
      "townhall_2026_q1.md is the script of the synthetic video; with the MP4 in the corpus the answer "
      "comes from a segment the worker heard, with start and end.",
      cite_kind="segment"),
    R("mm-04", "isolation", "What did the CFO say about EMEA revenue in the FY2026 town hall?",
      "zeta", [], [], False,
      "Only ACME holds the town hall - transcript or video. Zeta must refuse through every surface, "
      "the A2A peer included.", ["5.2 per cent"]),
    R("mm-05", "refusal", "What does Figure 7 of ACME's annual report show?",
      "acme", [], [], False,
      "The report references Figure 3 and no other figure. A model that describes a Figure 7 invented it."),
]

# ------------------------------------------------------------------------ verify then write
def corpus_text():
    out = {}
    for tenant in os.listdir(CORPUS):
        d = os.path.join(CORPUS, tenant)
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.endswith(".md"):
                out[(tenant, f.rsplit(".", 1)[0])] = open(
                    os.path.join(d, f), encoding="utf-8").read()
    return out


def main():
    if not os.path.isdir(CORPUS):
        sys.exit("run build_corpus.py first")
    text = corpus_text()
    # What the runner can match an anchor against: the document SLUG (which appears in
    # the chunk id and the source_uri) and the document TEXT (which carries clause ids
    # like EXP-12 and MSA-04). Anything else is an anchor nothing can ever satisfy.
    by_tenant = {}
    for (t, slug), body in text.items():
        by_tenant.setdefault(t, "")
        by_tenant[t] += "\n" + slug + "\n" + body

    problems = []
    for r in GOLDEN:
        t = r["tenant"]
        # an answerable row must be answerable FROM ITS OWN TENANT's documents
        for phrase in r["must_contain"]:
            if phrase.lower() not in by_tenant.get(t, "").lower():
                problems.append(f"{r['id']}: must_contain {phrase!r} is not in {t}'s corpus")
        for anchor in r["must_retrieve"]:
            if anchor.lower() not in by_tenant.get(t, "").lower():
                problems.append(f"{r['id']}: anchor {anchor!r} is not in {t}'s corpus")
        # THE CHECK THAT MAKES THESE REAL TESTS: every value an isolation row forbids
        # must actually exist in ANOTHER tenant's corpus. A must_not_contain nobody
        # could ever leak is a row that passes whether or not the filter works.
        for leak in r.get("must_not_contain", []):
            in_own = leak.lower() in by_tenant.get(t, "").lower()
            elsewhere = [o for o in by_tenant
                         if o != t and leak.lower() in by_tenant[o].lower()]
            if not elsewhere:
                problems.append(
                    f"{r['id']}: must_not_contain {leak!r} exists in no other tenant - "
                    f"this row cannot fail")
            if in_own:
                problems.append(
                    f"{r['id']}: must_not_contain {leak!r} is in {t}'s OWN corpus - "
                    f"answering it correctly would fail the row")
        if r["shape"] == "isolation" and not r.get("must_not_contain"):
            problems.append(f"{r['id']}: an isolation row needs a must_not_contain")

    shapes = {}
    for r in GOLDEN:
        shapes[r["shape"]] = shapes.get(r["shape"], 0) + 1
    print("golden.jsonl")
    print("  rows :", len(GOLDEN))
    print("  shape:", ", ".join(f"{k}={v}" for k, v in sorted(shapes.items())))
    print("  tenants:", ", ".join(sorted({r["tenant"] for r in GOLDEN})))
    ids = [r["id"] for r in GOLDEN]
    assert len(ids) == len(set(ids)), "duplicate row id"
    if problems:
        print("\n  ASSERTIONS THAT DO NOT MATCH THE CORPUS:")
        for p in problems:
            print("   !", p)
        sys.exit(1)
    print("  every must_contain / must_retrieve verified against corpus/  OK")
    iso = [r for r in GOLDEN if r["shape"] == "isolation"]
    print(f"  {len(iso)} isolation rows, each forbidding a value that exists in another")
    print("  tenant and NOT in its own - so each can actually fail:")
    for r in iso:
        for leak in r["must_not_contain"]:
            # match on the WHOLE value - splitting "1,84,500" on commas gives "1",
            # which is in every document and makes the report say nothing.
            owner = [o for o in by_tenant
                     if o != r["tenant"] and leak.lower() in by_tenant[o].lower()]
            print(f"    {r['id']}  asked as {r['tenant']:7} must not leak "
                  f"{leak:12} (lives in {'/'.join(owner)})")

    p = os.path.join(HERE, "golden.jsonl")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        for r in GOLDEN:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\nwrote {len(GOLDEN)} rows -> {p}")


if __name__ == "__main__":
    main()
