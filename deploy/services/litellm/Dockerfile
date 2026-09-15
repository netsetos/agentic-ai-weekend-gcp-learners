FROM docker.litellm.ai/berriai/litellm:v1.80.5-stable

WORKDIR /app

# Presidio + the Google Cloud SDKs the custom guardrail imports. The pins live in
# requirements.txt, where the dry run checks them, not in an inline list here.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download spaCy model for Presidio NER
RUN python -m spacy download en_core_web_lg

# The guardrail, the token proxy that fronts the Cloud Run backends, and the one config; entrypoint.sh
# starts the proxy beside litellm.
COPY documind_classifier.py documind_router.py dlp_audit.py gcp_id_token.py token_proxy.py ./
COPY config.yaml ./
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV PORT=8080
EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
