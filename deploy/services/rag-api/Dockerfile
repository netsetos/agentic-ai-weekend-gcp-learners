FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tini ca-certificates && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --shell /bin/bash --uid 10001 app
WORKDIR /app
# BUILD CONTEXT IS deploy/, not deploy/services/rag-api/ - this service imports the answer
# contract (and, in 12.8, the IAP verifier) from shared/, so shared/ has to land in the image:
#     gcloud builds submit --config=cloudbuild.yaml .      # run from deploy/
# chat, ingest and admin already build this way.
COPY services/rag-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=app:app shared/ ./shared/
COPY --chown=app:app services/rag-api/ ./
USER app
ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=20s --timeout=3s --start-period=15s CMD python -c "import urllib.request as r; r.urlopen('http://localhost:8080/health').read()" || exit 1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "2", "-b", "0.0.0.0:8080", "-t", "120", "--access-logfile", "-", "main:app"]
