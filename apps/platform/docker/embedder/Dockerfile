# Baked-model code-embedding image for a scale-to-zero Cloud Run deploy.
#
# The stock text-embeddings-inference (TEI) image downloads the model from the
# Hugging Face hub on first start. Under scale-to-zero that download runs on
# every cold start (each new instance after idle), adding tens of seconds and a
# network dependency to the request that triggered the wake. This image bakes the
# weights in at build time so a cold start only loads them from local disk.
#
# Build arg MODEL_ID selects what gets baked; it must match CODE_EMBEDDER_MODEL /
# CODE_EMBEDDER_MODEL_ID in the platform env (the vector's provenance tag). The
# default is jinaai/jina-embeddings-v2-base-code — the model deployed on the
# scale-to-zero CPU Cloud Run service (161M, 768-dim, code-trained, cheap on CPU).
# Qwen/Qwen3-Embedding-0.6B is higher quality but impractical on CPU (TEI/Candle
# materializes full f32 attention: it needs 16 GiB and truncated inputs), so it is
# only worth building for a GPU backend.
#
# TEI base pinned to cpu-1.8.2+: fixes an Intel-MKL crash on AMD hosts (TEI issues
# #636 / #667) that also affects a Qwen3 build; a scale-to-zero cold start can land
# on any CPU, so an older tag would risk a crash-loop.

# --- stage 1: fetch the weights with the HF CLI (not present in the TEI image) ---
FROM python:3.11-slim AS fetch

ARG MODEL_ID=jinaai/jina-embeddings-v2-base-code
ARG MODEL_REVISION=main

RUN pip install --no-cache-dir "huggingface_hub[cli]==0.25.2"

# Pull the full repo snapshot into /model. TEI serves a local directory the same
# way it serves a hub id, so we point --model-id at this path in stage 2.
RUN huggingface-cli download "$MODEL_ID" \
    --revision "$MODEL_REVISION" \
    --local-dir /model \
    --local-dir-use-symlinks False

# --- stage 2: the TEI runtime with the model already on disk ---
FROM ghcr.io/huggingface/text-embeddings-inference:cpu-1.8.2

COPY --from=fetch /model /model

# Cloud Run sends requests to $PORT (default 8080) and health-checks the same
# port; TEI's default is 80, so bind explicitly. --model-id /model serves the
# baked snapshot with no hub call. 0.0.0.0 is TEI's default host but we set it
# for clarity against Cloud Run's ingress.
ENV PORT=8080
ENTRYPOINT ["text-embeddings-router"]
CMD ["--model-id", "/model", "--hostname", "0.0.0.0", "--port", "8080"]
