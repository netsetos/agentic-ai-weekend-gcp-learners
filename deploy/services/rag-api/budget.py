"""The spend fraction the breaker reads, and the counter that feeds it. Module 10 (10.3), hand-written.

breakers.py has said since 12.6 what the service does at 80% and 100% of budget, and read a spend it
had no source for. tenant_daily (the BigQuery view over the log sink) answers it a day late, so the API
keeps the month's total itself: one Firestore document per month, incremented
by the cost of every answer it logs (main.py), read by choose_model_for() before it picks a tier.
SPEND_PCT overrides the reading - the replay: "what does the lane do at 85%?" without spending it.
"""
from __future__ import annotations

import datetime as dt

from google.cloud import firestore


def _month(db: firestore.Client):
    return db.collection("budget").document(dt.datetime.now(dt.timezone.utc).strftime("%Y-%m"))


def record(db: firestore.Client, usd: float) -> None:
    """Add one answer's cost to the month. Increment is atomic; forty concurrent answers do not race."""
    if usd > 0:
        _month(db).set({"usd": firestore.Increment(usd)}, merge=True)


def spend_pct(db: firestore.Client, cap_usd: float, override: str = "") -> float:
    """Percent of the month's cap spent so far; the override wins when set."""
    if override:
        return float(override)
    if not cap_usd:
        return 0.0
    snap = _month(db).get()
    usd = float((snap.to_dict() or {}).get("usd", 0.0)) if snap.exists else 0.0
    return 100.0 * usd / cap_usd
