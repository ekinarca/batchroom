# Batchroom — self-hosted AI media processing server
# Single-stage build: every dependency ships as a wheel on amd64 + arm64,
# so no compilers are needed and the image stays small. Models are NOT
# baked in — they download on demand (checksum-verified) into /data/models.

FROM python:3.12-slim

LABEL org.opencontainers.image.title="Batchroom" \
      org.opencontainers.image.description="Self-hosted batch background removal, resize, convert & preset packs. No meters, no quotas, no cloud." \
      org.opencontainers.image.source="https://github.com/ekinarca/batchroom" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BATCHROOM_DATA_DIR=/data \
    BATCHROOM_HOST=0.0.0.0 \
    BATCHROOM_PORT=5151

WORKDIR /app

# Install the package first as its own layer for build caching.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . \
    && useradd --uid 1000 --create-home batchroom \
    && mkdir -p /data /watch /output \
    && chown -R batchroom:batchroom /data /watch /output

USER batchroom

VOLUME ["/data"]
EXPOSE 5151

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('BATCHROOM_PORT','5151')}/api/health\", timeout=4).status == 200 else 1)"]

CMD ["batchroom", "serve"]
