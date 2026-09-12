from google.cloud import dlp_v2, bigquery
import json, datetime, os, random, asyncio
import logging
logger = logging.getLogger("documind.dlp_audit")

dlp_client = dlp_v2.DlpServiceClient()
bq_client = bigquery.Client()
# The project comes from the environment the gateway is deployed with (VERTEXAI_PROJECT / GOOGLE_CLOUD_PROJECT); the
# kit's copy carried a placeholder string until Module 11 joined the lane, so the template path could never resolve.
PROJECT_ID = os.environ.get("VERTEXAI_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT", "your-project-id")
DLP_TEMPLATE = f"projects/{PROJECT_ID}/locations/global/inspectTemplates/documind-pii"

def create_documind_inspect_template():
    """One-time setup: create inspect template with India info types."""
    template = {
        "inspect_config": {
            "info_types": [
                {"name": "EMAIL_ADDRESS"},
                {"name": "PHONE_NUMBER"},
                {"name": "CREDIT_CARD_NUMBER"},
                {"name": "US_SOCIAL_SECURITY_NUMBER"},
                {"name": "INDIA_AADHAAR_INDIVIDUAL"},
                {"name": "INDIA_PAN_INDIVIDUAL"},
                {"name": "INDIA_GST_INDIVIDUAL"},
                {"name": "PERSON_NAME"},
                {"name": "MEDICAL_TERM"},
                {"name": "DATE_OF_BIRTH"},
            ],
            "min_likelihood": dlp_v2.Likelihood.POSSIBLE,
            "include_quote": False,  # Don't include raw PII in findings
        },
        "display_name": "DocuMind PII Audit Template",
        "description": "Audit layer for hybrid routing validation",
    }
    return dlp_client.create_inspect_template(
        request={
            "parent": f"projects/{PROJECT_ID}/locations/global",
            "inspect_template": template,
            "template_id": "documind-pii"
        }
    )

async def async_audit_to_bigquery(request_id: str, text: str,
                                    classified_tier: str,
                                    sample_rate: float = 0.1):
    """Fire-and-forget DLP scan + BigQuery log.
    Only 10% of requests get DLP scanned (cost control).
    Findings compared against classified_tier to measure accuracy."""
    if random.random() > sample_rate:
        return
    
    try:
        # Scan with Cloud DLP
        response = await asyncio.to_thread(
            dlp_client.inspect_content,
            request={
                "parent": f"projects/{PROJECT_ID}/locations/global",
                "inspect_template_name": DLP_TEMPLATE,
                "item": {"value": text[:10000]}  # DLP has size limits
            }
        )
        
        findings = response.result.findings
        dlp_entities = [f.info_type.name for f in findings]
        
        # Determine DLP-inferred tier
        restricted_types = {"INDIA_AADHAAR_INDIVIDUAL", "INDIA_PAN_INDIVIDUAL",
                            "CREDIT_CARD_NUMBER", "US_SOCIAL_SECURITY_NUMBER",
                            "MEDICAL_TERM"}
        dlp_tier = ("RESTRICTED" if any(e in restricted_types for e in dlp_entities)
                    else "CONFIDENTIAL" if dlp_entities
                    else "PUBLIC")
        
        # Log for accuracy measurement
        bq_client.insert_rows_json(
            f"{PROJECT_ID}.documind.routing_audit",
            [{
                "request_id": request_id,
                "classified_tier": classified_tier,
                "dlp_tier": dlp_tier,
                "dlp_entities": dlp_entities,
                "match": classified_tier == dlp_tier,
                "timestamp": datetime.datetime.utcnow().isoformat(),
            }]
        )
    except Exception as e:
        logger.error(f"DLP audit failed: {e}")
