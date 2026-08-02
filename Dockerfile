FROM python:3.11-slim-trixie

WORKDIR /app

# Use the Tsinghua TUNA mirror. deb.debian.org throttles Docker Desktop NAT
# traffic to ~1 MB/s and 503s on the LibreOffice packages; the previously-used
# Aliyun mirror now fails to fetch fonts-noto-cjk outright (the build's
# recurring blocker). Measured fonts-noto-cjk download from a slim-trixie
# container: huaweicloud 1s, ustc/tuna 2s, tencent 123s, aliyun FAILED. TUNA is
# fast and reliable (USTC was also fine but has historically 403'd the Docker
# NAT IP).
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources

# Install OS-level deps required by Playwright, PyMuPDF, and legacy Office conversion.
# Several packages use the Debian 13 t64 (time_t-64) naming.
# Split into three layers to keep dpkg's peak memory low enough for constrained
# Docker Desktop builders (single-shot install of LibreOffice + CJK fonts OOMs at 12 GB).
RUN apt-get update && apt-get install -y --no-install-recommends -o Acquire::Retries=5 -o Acquire::http::Timeout=60 \
        curl \
        wget \
        gnupg \
        ca-certificates \
        libgl1 \
        libglib2.0-0t64 \
        libgomp1 \
        libnss3 \
        libnspr4 \
        libdbus-1-3 \
        libatk1.0-0t64 \
        libatk-bridge2.0-0t64 \
        libcups2t64 \
        libdrm2 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxrandr2 \
        libxrender1 \
        libgbm1 \
        libasound2t64 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y --no-install-recommends -o Acquire::Retries=5 -o Acquire::http::Timeout=60 \
        libreoffice-writer \
        libreoffice-impress \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y --no-install-recommends -o Acquire::Retries=5 -o Acquire::http::Timeout=60 \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt requirements-ocr-linux-x86_64.txt requirements-ocr-arm64.txt ./
COPY scripts/install_ocr_deps.sh ./scripts/install_ocr_deps.sh
# Local-OCR variant switch:
#   WITH_LOCAL_OCR=true  (default) — bundle the offline PaddleOCR stack
#     (paddlepaddle / opencv / onnxruntime, ~1 GB uncompressed).
#   WITH_LOCAL_OCR=false — skip it, for a ~1.5 GB smaller image. MANTISFETCH_LOCAL_OCR_ENABLED
#     is baked to match so OCR routing skips the (absent) local worker entirely and
#     uses the configured LLM/vision provider — including PDF page OCR, which has no
#     per-page local→LLM fallback of its own. An LLM provider is required to OCR.
ARG WITH_LOCAL_OCR=true
ENV MANTISFETCH_LOCAL_OCR_ENABLED=${WITH_LOCAL_OCR}
LABEL com.readyforai.mantisfetch.local-ocr="${WITH_LOCAL_OCR}"
# build-essential is needed only to compile C-extension wheels (e.g. stringzilla,
# which no longer ships a prebuilt wheel) during pip install. Install it in this
# same layer and purge it afterward so the compiler toolchain never ships in the
# runtime image (keeps it slim + smaller attack surface).
RUN apt-get update && apt-get install -y --no-install-recommends -o Acquire::Retries=5 -o Acquire::http::Timeout=60 build-essential \
    && pip install --no-cache-dir -r requirements.txt \
    && if [ "$WITH_LOCAL_OCR" = "true" ]; then sh ./scripts/install_ocr_deps.sh; else echo "WITH_LOCAL_OCR=$WITH_LOCAL_OCR — skipping local OCR deps"; fi \
    && apt-get purge -y build-essential \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Install Playwright browser (Chromium only — smallest footprint)
RUN playwright install chromium

# Bake the default PaddleOCR weights (~22 MB) into the image. Without them the
# runtime fetches them from HuggingFace on the first OCR call, which makes the
# WITH_LOCAL_OCR=true variant *not* actually offline — the one thing it exists
# for. An air-gapped install fails outright, and even a connected one re-pays
# the download after every container recreate, since /root/.paddlex lives in the
# container layer rather than a volume.
#
# Go through the worker's own _build_engine() rather than naming models here, so
# the baked weights cannot drift from the ones the runtime asks for (it reads
# MANTISFETCH_LOCAL_OCR_{DET,REC}_MODEL, defaulting to PP-OCRv5_mobile_*). The
# file is copied on its own, ahead of the full source, so an unrelated source
# change does not invalidate this layer.
COPY services/docreader/paddle_ocr_worker.py ./services/docreader/paddle_ocr_worker.py
RUN if [ "$WITH_LOCAL_OCR" = "true" ]; then \
        python -c "import sys; sys.path.insert(0, 'services/docreader'); \
from paddle_ocr_worker import _build_engine; _build_engine()" \
        && du -sh /root/.paddlex; \
    else echo "WITH_LOCAL_OCR=$WITH_LOCAL_OCR — skipping OCR model bake"; fi

# Copy application source
COPY . .

EXPOSE 9898

CMD ["python", "mantisfetch_server.py"]
