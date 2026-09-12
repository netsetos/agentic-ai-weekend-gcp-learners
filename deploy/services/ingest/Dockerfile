# syntax=docker/dockerfile:1.7
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tini ca-certificates && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --shell /bin/bash --uid 10001 app
WORKDIR /app
COPY services/ingest/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=app:app shared/ ./shared/
COPY --chown=app:app services/ingest/ .
USER app
ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
# CONCURRENCY 1, and one worker. The process holds a document claim while it
# parses and embeds; a second request on the same instance contends for CPU and
# stretches the ack window, and Pub/Sub redelivers whatever is not acked in
# time - which is how one slow instance turns one document into three
# deliveries and three claims.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "1", "-b", "0.0.0.0:8080", "-t", "600", "--access-logfile", "-", "main:app"]
