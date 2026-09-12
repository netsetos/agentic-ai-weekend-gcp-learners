"""What the service DOES when the money runs out."""
import os

# Two thresholds, two different actions, and neither of them is an email.
#
#   80%  -> STRICT ROUTING. router.py stops sending anything to Pro; every
#           request goes to flash-lite unless it is classified COMPLEX. The
#           service gets cheaper and slightly worse, and stays up.
#
#   100% -> MIN-INSTANCES 0. The service scales to zero when idle. Cold starts
#           come back, p95 rises, and the bill stops growing. Still up.
#
# What neither does is turn DocuMind off. A budget alert that takes the service
# down converts a finance problem into an outage, and the people who set the
# budget are never the people who wanted that.
BUDGET_STRICT_PCT = 80
BUDGET_FLOOR_PCT = 100


def routing_mode(spend_pct: float) -> str:
    return "strict" if spend_pct >= BUDGET_STRICT_PCT else "normal"


def choose_model(question_class: str, spend_pct: float) -> str:
    """router.py, wired to the budget.

    Until now router.py shipped in the image and nothing imported it - the
    classifier existed and never chose anything.
    """
    if routing_mode(spend_pct) == "strict":
        return ("gemini-3.6-flash" if question_class == "COMPLEX"
                else "gemini-3.1-flash-lite")
    # router.py's classes are SIMPLE / MEDIUM / COMPLEX; this table said MODERATE, so a medium question
    # fell through to flash-lite the day the two were first joined (Module 10). Both spellings answer.
    return {"COMPLEX": "gemini-3.1-pro-preview",
            "MEDIUM": "gemini-3.6-flash",
            "MODERATE": "gemini-3.6-flash"}.get(question_class,
                                                "gemini-3.1-flash-lite")
