FROM python:3.12-slim
RUN useradd --create-home --shell /bin/bash --uid 10001 app
WORKDIR /app
# BUILD CONTEXT IS deploy/, not deploy/services/mcp/ - the server imports the shared layer
# (the one retrieve(), the bearer-token verifier, the roster), so shared/ lands beside it:
#     gcloud builds submit . --config=cloudbuild.yaml \
#       --substitutions=_IMAGE=...,_DOCKERFILE=services/mcp/Dockerfile      # run from deploy/
COPY services/mcp/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=app:app shared/ ./shared/
COPY --chown=app:app services/mcp/ ./
USER app
ENV PYTHONUNBUFFERED=1 FASTMCP_STATELESS_HTTP=true
# Cloud Run sets PORT. Stateless streamable HTTP: no session to pin, so no --session-affinity.
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080}"]
