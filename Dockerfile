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
# build-essential is needed only to compile C-extension wheels (e.g. stringzilla,
# which no longer ships a prebuilt wheel) during pip install. Install it in this
# same layer and purge it afterward so the compiler toolchain never ships in the
# runtime image (keeps it slim + smaller attack surface).
RUN apt-get update && apt-get install -y --no-install-recommends -o Acquire::Retries=5 -o Acquire::http::Timeout=60 build-essential \
    && pip install --no-cache-dir -r requirements.txt \
    && sh ./scripts/install_ocr_deps.sh \
    && apt-get purge -y build-essential \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Install Playwright browser (Chromium only — smallest footprint)
RUN playwright install chromium

# Copy application source
COPY . .

EXPOSE 9898

CMD ["python", "mantisfetch_server.py"]
