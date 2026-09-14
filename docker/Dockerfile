# The -cudnn- flavour is required: CTranslate2 4.8.2 links against cuDNN 9 at runtime and the
# plain nvidia/cuda runtime image does not ship it.
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /usr/local/bin/uv

ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# Ubuntu 24.04 ships Python 3.12; the project requires 3.14, so uv provides the interpreter.
RUN uv python install 3.14

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --extra server --no-install-project

COPY README.md ./
COPY src ./src
COPY prompt.md dictation.md ./
COPY config ./config
RUN uv sync --locked --no-dev --extra server

ENV PATH="/app/.venv/bin:$PATH" \
    VIRTUAL_ENV=/app/.venv \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/cache/huggingface \
    XDG_CACHE_HOME=/cache \
    CUDA_CACHE_PATH=/cache/ctranslate2/nv

# CUDA_CACHE_PATH lives in a named volume on purpose: CTranslate2 4.8.2 predates Blackwell
# (sm_120), so its kernels are JIT-compiled on first use - about 6s that would otherwise be
# paid again by the first voice request after every container recreate.
RUN mkdir -p /cache/huggingface /cache/ctranslate2/nv && chmod -R 0777 /cache

EXPOSE 8765

HEALTHCHECK --interval=10s --timeout=5s --start-period=180s --retries=30 \
    CMD ["python", "-c", "import json,sys,urllib.request;sys.exit(0 if json.load(urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=4)).get('status') == 'ready' else 1)"]

CMD ["python", "-m", "vox_server.main"]
