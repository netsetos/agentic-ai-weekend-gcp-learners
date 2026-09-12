# syntax=docker/dockerfile:1.7
# Built from the deploy/ directory, not services/admin/, so shared/ ships with it:
#   docker build -f services/admin/Dockerfile -t admin .
FROM python:3.12-slim
RUN useradd --create-home --shell /bin/bash --uid 10001 app
WORKDIR /app
COPY services/admin/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=app:app shared/ ./shared/
COPY --chown=app:app services/admin/ .
USER app
ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["streamlit", "run", "app.py", "--server.port=8080", "--server.address=0.0.0.0", "--server.headless=true"]
