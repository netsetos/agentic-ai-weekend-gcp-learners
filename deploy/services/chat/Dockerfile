FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tini ca-certificates && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --shell /bin/bash --uid 10001 app
WORKDIR /app
# BUILD CONTEXT IS deploy/, not deploy/services/chat/ - the service imports the shared tool
# layer, so shared/ has to land in the image beside it:
#     docker build -f services/chat/Dockerfile -t documind-chat .      # run from deploy/
COPY services/chat/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=app:app shared/ ./shared/
COPY --chown=app:app services/chat/ ./
USER app
ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=20s --timeout=3s --start-period=15s CMD python -c "import urllib.request as r; r.urlopen('http://localhost:8080/health').read()" || exit 1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "2", "-b", "0.0.0.0:8080", "-t", "120", "--access-logfile", "-", "agent:app"]
