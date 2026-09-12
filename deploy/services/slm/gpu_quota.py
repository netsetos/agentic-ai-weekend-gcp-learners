#!/usr/bin/env python3
"""Cap the project's Cloud Run GPU quota - the ceiling below every --max-instances. Cost control, 10 September 2026.

    python services/slm/gpu_quota.py --project P                     # show every GPU-shaped quota of run.googleapis.com
    python services/slm/gpu_quota.py --project P --cap 1             # override the L4 quotas for --region to 1 (make gpu-cap)
    python services/slm/gpu_quota.py --project P --remove            # delete the overrides this tool created (make gpu-cap-off)
    python services/slm/gpu_quota.py --selftest

Why a quota and not only --max-instances 1: max-instances is a property of ONE service. A second GPU service, a
candidate revision with a GPU, or a typo deploys another card, and the bill for two L4 instances is Rs 173,808 a
month (11.4). A consumer quota override is the project's ceiling for the region: Cloud Run refuses to allocate past
it, lowering it needs no approval (raising it does), and it survives every deploy. Service Usage v1beta1:
consumerQuotaMetrics -> consumerQuotaLimits -> consumerOverrides, with force=true because a decrease of more than
10% is otherwise refused as a safety check - here the decrease is the point. The metric names are read from the
project, never typed (an unknown name fails the call): 11.1's Cell 3 reads the same list.
"""
from __future__ import annotations

import argparse
import sys
import time

API = "https://serviceusage.googleapis.com/v1beta1"


def gpu_metrics(metrics: list[dict]) -> list[dict]:
    """The GPU-shaped quota metrics of a service: the metric or its display name says gpu (the L4 lines among them)."""
    return [m for m in metrics if "gpu" in (m.get("metric", "") + " " + m.get("displayName", "")).lower()]


def bucket_for(limit: dict, region: str) -> dict | None:
    """The quota bucket that applies in `region`: the region's own when the limit is per region, else the default."""
    buckets = limit.get("quotaBuckets") or []
    if "{region}" in limit.get("unit", ""):
        own = next((b for b in buckets if (b.get("dimensions") or {}).get("region") == region), None)
        if own:
            return own
    return next((b for b in buckets if not b.get("dimensions")), None)


def override_body(limit: dict, cap: int, region: str) -> dict:
    """The consumerOverride to create: the value, and the region dimension when the limit is per region."""
    body = {"overrideValue": str(cap)}
    if "{region}" in limit.get("unit", ""):
        body["dimensions"] = {"region": region}
    return body


def describe(bucket: dict | None) -> str:
    if not bucket:
        return "?"
    ov = bucket.get("consumerOverride") or {}
    return f"effective {bucket.get('effectiveLimit', '?')} (default {bucket.get('defaultLimit', '?')}" + (f", override {ov.get('overrideValue')}" if ov else "") + ")"


# ----------------------------------------------------------------------------- the live pass
def _session():
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(creds)


def _number(sess, project: str) -> str:
    r = sess.get(f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}", timeout=60)
    r.raise_for_status()
    return r.json()["projectNumber"]


def _metrics(sess, number: str, service: str) -> list[dict]:
    out, token = [], None
    while True:
        params = {"view": "FULL", "pageSize": 500}
        if token:
            params["pageToken"] = token
        r = sess.get(f"{API}/projects/{number}/services/{service}/consumerQuotaMetrics", params=params, timeout=120)
        r.raise_for_status()
        j = r.json()
        out += j.get("metrics", [])
        token = j.get("nextPageToken")
        if not token:
            return out


def _wait(sess, op: dict) -> dict:
    name = op.get("name")
    while name and not op.get("done"):
        time.sleep(2)
        op = sess.get(f"{API}/{name}", timeout=60).json()
    if op.get("error"):
        raise RuntimeError(op["error"])
    return op


def live(args) -> int:
    sess = _session()
    number = _number(sess, args.project)
    gpu = gpu_metrics(_metrics(sess, number, args.service))
    if not gpu:
        print(f"no GPU-shaped quota metric is visible on {args.service} for {args.project}: nothing to cap, and the first --gpu deploy is the quota test")
        return 2
    changed = 0
    for m in gpu:
        print(f"\n{m.get('displayName', m['metric'])}\n  {m['metric']}")
        for lim in m.get("consumerQuotaLimits", []):
            b = bucket_for(lim, args.region)
            print(f"  {lim.get('unit', '?'):28} {args.region if '{region}' in lim.get('unit', '') else 'all regions':12} {describe(b)}")
            if args.cap is not None:
                ov = (b or {}).get("consumerOverride") or {}
                if ov.get("overrideValue") == str(args.cap):
                    print(f"  {'':28} already capped at {args.cap}")
                    continue
                r = sess.post(f"{API}/{lim['name']}/consumerOverrides", params={"force": "true"}, json=override_body(lim, args.cap, args.region), timeout=120)
                if r.status_code != 200:
                    print(f"  {'':28} refused: HTTP {r.status_code} {r.text[:200]}")
                    continue
                _wait(sess, r.json())
                changed += 1
                print(f"  {'':28} -> capped at {args.cap}")
            elif args.remove:
                ov = (b or {}).get("consumerOverride") or {}
                if not ov.get("name"):
                    continue
                r = sess.delete(f"{API}/{ov['name']}", params={"force": "true"}, timeout=120)
                if r.status_code != 200:
                    print(f"  {'':28} refused: HTTP {r.status_code} {r.text[:200]}")
                    continue
                _wait(sess, r.json())
                changed += 1
                print(f"  {'':28} -> override removed (back to the default)")
    if args.cap is not None or args.remove:
        print(f"\n{changed} override(s) written. Read back:")
        for m in gpu_metrics(_metrics(sess, number, args.service)):
            for lim in m.get("consumerQuotaLimits", []):
                print(f"  {m.get('displayName', m['metric'])[:48]:48} {lim.get('unit', '?'):26} {describe(bucket_for(lim, args.region))}")
    return 0


# ----------------------------------------------------------------------------- selftest
def selftest() -> int:
    """The pure parts, on a fixture that can go red: which metrics count as GPU-shaped, which bucket applies, what
    override is written."""
    regional = {"name": "projects/1/services/run.googleapis.com/consumerQuotaMetrics/x/limits/y", "unit": "1/{project}/{region}",
                "quotaBuckets": [{"effectiveLimit": "3", "defaultLimit": "3"},
                                 {"effectiveLimit": "1", "defaultLimit": "3", "dimensions": {"region": "us-central1"},
                                  "consumerOverride": {"name": "projects/1/.../consumerOverrides/abc", "overrideValue": "1"}}]}
    global_limit = {"name": "projects/1/services/run.googleapis.com/consumerQuotaMetrics/x/limits/z", "unit": "1/{project}",
                    "quotaBuckets": [{"effectiveLimit": "10", "defaultLimit": "10"}]}
    metrics = [
        {"metric": "run.googleapis.com/nvidia_l4_gpu_allocation_no_zonal_redundancy", "displayName": "Total NVIDIA L4 GPU allocation without zonal redundancy",
         "consumerQuotaLimits": [regional]},
        {"metric": "run.googleapis.com/cpu_allocation", "displayName": "Total CPU allocation", "consumerQuotaLimits": [global_limit]},
        {"metric": "run.googleapis.com/gpu_allocation_with_zonal_redundancy", "displayName": "Total GPU allocation", "consumerQuotaLimits": [global_limit]},
    ]
    picked = gpu_metrics(metrics)
    assert [m["metric"] for m in picked] == [metrics[0]["metric"], metrics[2]["metric"]], picked
    assert bucket_for(regional, "us-central1")["effectiveLimit"] == "1", "the region's own bucket applies"
    assert bucket_for(regional, "asia-south1")["effectiveLimit"] == "3", "a region without a bucket reads the default"
    assert bucket_for(global_limit, "us-central1")["effectiveLimit"] == "10"
    assert override_body(regional, 1, "us-central1") == {"overrideValue": "1", "dimensions": {"region": "us-central1"}}
    assert override_body(global_limit, 1, "us-central1") == {"overrideValue": "1"}, "a limit without a region dimension takes none"
    assert describe(bucket_for(regional, "us-central1")) == "effective 1 (default 3, override 1)"
    assert describe(None) == "?"
    print("selftest OK - GPU-shaped metrics picked by name, the region's bucket read before the default, the override carries the region only when the limit does")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project")
    ap.add_argument("--region", default="us-central1")
    ap.add_argument("--service", default="run.googleapis.com")
    ap.add_argument("--cap", type=int, help="write a consumer override of this value on every GPU-shaped limit for --region")
    ap.add_argument("--remove", action="store_true", help="delete the overrides on those limits")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.project:
        print("--project is required (or --selftest)", file=sys.stderr)
        return 2
    return live(args)


if __name__ == "__main__":
    raise SystemExit(main())
