"""What one answer cost, in the currency the person paying thinks in."""
import os
from functools import lru_cache

from google.cloud import bigquery

USD_INR = float(os.environ.get("USD_INR_RATE", "85"))
# Fallback only. The table is the source of truth: a price change should be a
# row, not a redeploy, because rates move faster than release trains.
FALLBACK = {"gemini-3.6-flash": (1.50, 7.50),
            "gemini-3.1-flash-lite": (0.25, 1.50),
            "gemini-3.1-pro-preview": (2.00, 12.00),
            # Module 11's gateway routes (services/litellm/config.yaml, compare_backends.py's PRICES). The self-hosted
            # ones are a RATE, not a price: Rs 86,904 a month for the L4 instance over ~50M tokens - it only holds at
            # that volume. The gateway's own x-litellm-response-cost header wins when the answer carries it.
            "documind-general": (1.50, 7.50),
            "documind-reasoning": (2.00, 12.00),
            "documind-slm": (20.5, 20.5),
            "documind-inference": (20.5, 20.5),
            "documind-gke": (20.5, 20.5)}


@lru_cache(maxsize=1)
def _prices() -> dict:
    """model -> (usd_in, usd_out) per 1M tokens, from BigQuery."""
    try:
        rows = bigquery.Client().query(
            "SELECT model, usd_per_1m_in, usd_per_1m_out "
            "FROM `documind_observability.model_prices` "
            "WHERE CURRENT_DATE() BETWEEN valid_from AND valid_to").result()
        return {r.model: (r.usd_per_1m_in, r.usd_per_1m_out) for r in rows}
    except Exception:
        return FALLBACK


def price(model: str, tokens_in: int, tokens_out: int,
          cached_tokens: int = 0) -> dict:
    """Both currencies, always, and the cached tokens counted at the cache rate.

    Reporting only USD to an Indian finance team means somebody re-does the
    conversion in a spreadsheet at whatever rate they had to hand, and then two
    numbers exist for one month. Carry the rate WITH the figure.
    """
    # A tuned model (10.1) is an endpoint path, billed at its BASE model's rate: RAG_MODEL_BASE names it.
    if model.startswith("projects/"):
        model = os.environ.get("RAG_MODEL_BASE") or "gemini-3.6-flash"
    usd_in, usd_out = _prices().get(model, FALLBACK["gemini-3.6-flash"])
    billable_in = max(tokens_in - cached_tokens, 0)
    usd = (billable_in * usd_in
           + cached_tokens * usd_in * 0.10      # cached input: 90% off
           + tokens_out * usd_out) / 1_000_000
    return {"model": model, "usd": round(usd, 6),
            "inr": round(usd * USD_INR, 4), "usd_inr_rate": USD_INR,
            "tokens_in": tokens_in, "tokens_out": tokens_out,
            "cached_tokens": cached_tokens}
