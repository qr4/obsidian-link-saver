# Obsidian save service: web UI + API + workers that turn links into vault
# entries. GPU (NVIDIA) is used for Whisper transcription when available.

# --- SingleFile CLI (needs Node >= 24) ------------------------------------
FROM node:24-bookworm-slim AS singlefile
ARG SINGLEFILE_VERSION=2.10.0
RUN npm install -g --omit=dev "single-file-cli@${SINGLEFILE_VERSION}" && npm cache clean --force

# --- runtime ----------------------------------------------------------------
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VAULT_DIR=/vault \
    CONFIG_DIR=/config \
    HOME=/config \
    PORT=8765 \
    SINGLEFILE_BIN=/usr/local/bin/single-file \
    CHROMIUM_BIN=/usr/bin/chromium \
    YTDLP_EXTRA_ARGS="--js-runtimes node" \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

# chromium: page capture; ffmpeg: audio decoding + subtitle muxing;
# git: repo clones; tzdata: honour TZ for timestamps; tini: signal handling
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        chromium ffmpeg git ca-certificates tzdata tini fonts-liberation \
 && rm -rf /var/lib/apt/lists/*

# Node 24 also serves as yt-dlp's JavaScript runtime for YouTube's challenges.
COPY --from=singlefile /usr/local/bin/node /usr/local/bin/node
COPY --from=singlefile /usr/local/lib/node_modules/single-file-cli /usr/local/lib/node_modules/single-file-cli
RUN ln -s /usr/local/lib/node_modules/single-file-cli/single-file-node.js /usr/local/bin/single-file

# Dependencies first, in their own layer (only pyproject.toml invalidates
# it), so a code change doesn't re-download ~1.5 GB of wheels. CUDA 12
# cuBLAS/cuDNN 9 come from NVIDIA's wheels (no CUDA base image needed); the
# host driver is injected by the NVIDIA container runtime.
# (Built from a throwaway /deps dir: setuptools leaves build/ in the source
# tree, and reusing that with the real sources would install the placeholder.)
COPY pyproject.toml /deps/
RUN mkdir -p /deps/saver && touch /deps/saver/__init__.py \
 && pip install "/deps[whisper]" "yt-dlp[default]" nvidia-cublas-cu12 "nvidia-cudnn-cu12>=9,<10" \
 && pip uninstall -y obsidian-save-service && rm -rf /deps
COPY pyproject.toml /src/
COPY saver /src/saver
RUN pip install --no-deps /src && rm -rf /src
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh && mkdir -p /config /vault

WORKDIR /config
EXPOSE 8765
VOLUME ["/config"]
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
    CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8765\")}/api/health',timeout=5)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "saver", "serve"]
