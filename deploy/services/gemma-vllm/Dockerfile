FROM vllm/vllm-openai:v0.28.0

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
EXPOSE 8080

# Reset the vllm-openai base image ENTRYPOINT so our uvicorn CMD runs as the command
ENTRYPOINT []

# --workers 1 MANDATORY - GPU memory not shareable across processes
CMD ["python", "-m", "uvicorn", "main:app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", "--timeout-keep-alive", "120"]
