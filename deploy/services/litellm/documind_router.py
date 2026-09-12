from litellm.integrations.custom_guardrail import CustomGuardrail
from documind_classifier import classify_tier, SensitivityTier
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
import logging
import os

logger = logging.getLogger("documind.router")

class DocuMindRouter(CustomGuardrail):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.analyzer = AnalyzerEngine()
        self.anonymizer = AnonymizerEngine()
    
    def mask_pii(self, data: dict) -> dict:
        """Replace PII with placeholders before sending to external API."""
        for msg in data.get("messages", []):
            content = msg.get("content")
            if not isinstance(content, str):
                continue
            results = self.analyzer.analyze(text=content, language="en")
            if results:
                anonymized = self.anonymizer.anonymize(text=content, analyzer_results=results)
                msg["content"] = anonymized.text
        return data
    
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        """Runs BEFORE LiteLLM dispatches the request."""
        # Concatenate all user messages
        text = " ".join(
            m.get("content", "") for m in data.get("messages", [])
            if isinstance(m.get("content"), str)
        )
        
        tier = classify_tier(text)
        original_model = data.get("model")
        
        # Routing decision. ROUTER_ENFORCE=0 is SHADOW MODE (11.3): classify and log every request, change nothing -
        # the safe rollout for a classifier that moves traffic. The variable lives on the gateway revision
        # (make deploy-gateway ROUTER_ENFORCE=0) and is read per call, so a notebook can replay it.
        enforce = os.environ.get("ROUTER_ENFORCE", "1") != "0"
        if tier == SensitivityTier.RESTRICTED:
            if enforce:
                data["model"] = "documind-sensitive"
            logger.info(f"RESTRICTED routed to self-hosted (was {original_model}, enforce={enforce})")
        elif tier == SensitivityTier.CONFIDENTIAL:
            if enforce:
                data = self.mask_pii(data)
                data["model"] = "documind-general"
            logger.info(f"CONFIDENTIAL masked and routed to general (enforce={enforce})")
        # else PUBLIC: keep original model selection
        
        # Log routing decision for audit trail
        data["metadata"] = data.get("metadata", {})
        data["metadata"]["routing_tier"] = tier.name
        data["metadata"]["original_model"] = original_model
        data["metadata"]["enforced"] = enforce
        return data
