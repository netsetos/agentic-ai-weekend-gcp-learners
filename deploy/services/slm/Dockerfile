# documind-slm: 10.5's fine-tuned Gemma, served by Ollama on Cloud Run L4.
# Owned by lesson 11.4.

# 0.33.3 (2 Sep 2026). This pin is not cosmetic: gemma4 support arrived in the
# 0.33.x line, and 10.5 fine-tunes unsloth/gemma-4-E2B-it. An older Ollama loads
# the GGUF and produces nonsense rather than refusing, which is the worst way to
# find out.
FROM ollama/ollama:0.33.3

ENV OLLAMA_HOST=0.0.0.0:8080
ENV OLLAMA_MODELS=/models
EXPOSE 8080

# Two builds, one image name (Module 11, decision D2). make deploy-slm stages build/ before the deploy:
#   the tuned model     build/documind-slm.gguf + build/Modelfile, fetched from gs://PROJECT-datasets/sft/ - the GGUF
#                       10.5 exported and the Modelfile it GENERATED from the tokenizer (never hand-written: a template
#                       that disagrees with the checkpoint produces fluent text that never stops)
#   the stand-in        build/STOCK naming a stock model (make deploy-slm SLM_STOCK=gemma3:4b), pulled at build time,
#                       until 10.5's file lands
# Either way Ollama serves the model as `documind-slm`, and the service's slm-source label says which one answered.
# The model is created at BUILD time, not at start-up: importing a 2 GB GGUF is time a cold start does not have.
COPY build/ /build/
RUN ollama serve & sleep 5 && \
    if [ -s /build/STOCK ]; then ollama pull "$(cat /build/STOCK)" && ollama cp "$(cat /build/STOCK)" documind-slm; \
    else ollama create documind-slm -f /build/Modelfile; fi && \
    pkill ollama

ENTRYPOINT ["ollama"]
CMD ["serve"]
