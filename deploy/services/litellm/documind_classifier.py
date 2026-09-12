import re
from enum import IntEnum
from presidio_analyzer import AnalyzerEngine

class SensitivityTier(IntEnum):
    PUBLIC = 0
    INTERNAL = 1
    CONFIDENTIAL = 2
    RESTRICTED = 3

PATTERNS_RESTRICTED = {
    "SSN": re.compile(r"\b(?!000|666|9\d{2})\d{3}[-\s]?\d{2}[-\s]?\d{4}\b"),
    "AADHAAR": re.compile(r"\b[2-9]\d{3}[-\s]?\d{4}[-\s]?\d{4}\b"),
    "PAN": re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"),
    "CREDIT_CARD": re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
}
PATTERNS_CONFIDENTIAL = {
    "EMAIL": re.compile(r"\b[\w.-]+@[\w.-]+\.\w+\b"),
    "PHONE": re.compile(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b"),
}

_analyzer = None
def get_analyzer():
    global _analyzer
    if _analyzer is None:
        _analyzer = AnalyzerEngine()
    return _analyzer

def classify_tier(text: str) -> SensitivityTier:
    # Layer 1: fast regex for RESTRICTED
    for name, pattern in PATTERNS_RESTRICTED.items():
        if pattern.search(text):
            # Layer 2: Presidio confirms context-aware
            results = get_analyzer().analyze(text=text, language="en")
            if any(r.score >= 0.7 for r in results):
                return SensitivityTier.RESTRICTED
    # Layer 1 for CONFIDENTIAL
    for name, pattern in PATTERNS_CONFIDENTIAL.items():
        if pattern.search(text):
            return SensitivityTier.CONFIDENTIAL
    return SensitivityTier.PUBLIC

# Quick test
test_cases = [
    ("What is GDPR?", SensitivityTier.PUBLIC),
    ("Email me at user@acme.com", SensitivityTier.CONFIDENTIAL),
    ("My Aadhaar is 2345-6789-0123", SensitivityTier.RESTRICTED),
    ("SSN 123-45-6789", SensitivityTier.RESTRICTED),
    ("PAN ABCDE1234F", SensitivityTier.RESTRICTED),
]
