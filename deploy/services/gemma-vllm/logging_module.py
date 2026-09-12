import re, datetime
from google.cloud import bigquery
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine

pii_analyzer = AnalyzerEngine()
pii_anonymizer = AnonymizerEngine()
bq_client = bigquery.Client()
BQ_TABLE = "project.dataset.inference_logs"

def redact_pii(text: str) -> str:
    """Presidio NER + regex fallback."""
    try:
        results = pii_analyzer.analyze(text=text, language="en")
        if results:
            return pii_anonymizer.anonymize(text=text, analyzer_results=results).text
    except Exception:
        pass
    # Regex safety net
    text = re.sub(r"\b[\w.-]+@[\w.-]+\.\w+\b", "<EMAIL>", text)
    text = re.sub(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b", "<PHONE>", text)
    text = re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "<SSN>", text)
    return text

async def log_request(entry: dict):
    """Non-blocking BigQuery streaming insert."""
    errors = bq_client.insert_rows_json(BQ_TABLE, [entry])
    if errors:
        print(f"BQ insert failed: {errors}")
