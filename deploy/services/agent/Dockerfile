FROM python:3.12-slim
RUN useradd --create-home --shell /bin/bash --uid 10001 app
WORKDIR /app
# BUILD CONTEXT IS deploy/, like every kit image (cloudbuild.yaml names the Dockerfile) - and
# this one copies ONLY its own directory. No shared/: the peer reaches the lane through the MCP
# server, never through the kit's retrieve(), and the absence is provable from the image:
#     docker run --rm IMAGE sh -c 'ls shared 2>/dev/null || echo "no kit inside"'
COPY services/agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=app:app services/agent/ ./
USER app
ENV PYTHONUNBUFFERED=1
# Cloud Run sets PORT. The A2A app is stateless per request; tasks live in memory for the
# life of a message, so no --session-affinity is needed.
CMD ["sh", "-c", "uvicorn agent:app --host 0.0.0.0 --port ${PORT:-8080}"]
