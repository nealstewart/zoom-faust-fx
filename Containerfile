# Podman Containerfile for Stomphacks + zoom-faust-fx Full Build & Pedal Server
# Platform MUST be linux/amd64 because TI C6000 CGT 8.5.0 LTS is x86_64 Linux only.
FROM --platform=linux/amd64 python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STOMPHACKS_DIR=/opt/stomphacks \
    ZOOM_TI_CGT=/opt/ti/c6000 \
    STOMPHACKS_PROFILE=podman

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    faust \
    git \
    libasound2 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Optional build-time TI C6000 CGT 8.5.0 LTS Linux x86_64 installer injection.
# Place ti_cgt_c6000_8.5.0.LTS_linux-x64_installer.bin in the build context
# to bake it into /opt/ti/c6000, or leave it absent and mount ti-cgt:/opt/ti/c6000:ro
# at runtime via compose.yaml.
ARG TI_CGT_INSTALLER=ti_cgt_c6000_8.5.0.LTS_linux-x64_installer.bin
COPY . /tmp/build-ctx/
RUN if [ -f "/tmp/build-ctx/${TI_CGT_INSTALLER}" ]; then \
        chmod +x "/tmp/build-ctx/${TI_CGT_INSTALLER}" && \
        "/tmp/build-ctx/${TI_CGT_INSTALLER}" --mode unattended --prefix /opt/ti/c6000 && \
        rm -f "/tmp/build-ctx/${TI_CGT_INSTALLER}"; \
    fi

# Prepare /opt/stomphacks:
# If vendor/stomphacks is present in the build context, seed /opt/stomphacks from it;
# otherwise clone stomphacks and zoom-zt2 (with PR #112 fix for 12-char filenames).
ARG STOMPHACKS_REPO=https://github.com/nealstewart/stomphacks.git
ARG STOMPHACKS_REF=main
RUN if [ -d "/tmp/build-ctx/vendor/stomphacks/tools" ]; then \
        mkdir -p /opt/stomphacks && \
        cp -a /tmp/build-ctx/vendor/stomphacks/. /opt/stomphacks/; \
    else \
        git clone --depth 1 --branch "${STOMPHACKS_REF}" "${STOMPHACKS_REPO}" /opt/stomphacks; \
    fi && \
    if [ ! -f "/opt/stomphacks/zoom-zt2/zoomzt2.py" ]; then \
        git clone --depth 1 https://github.com/shooking/zoom-zt2.git /opt/stomphacks/zoom-zt2 && \
        cd /opt/stomphacks/zoom-zt2 && \
        git fetch origin pull/112/head:pr-112 && \
        git checkout pr-112 || true; \
    fi && \
    rm -rf /tmp/build-ctx

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir -e ".[server]" && \
    mkdir -p /app/session /app/backup /app/build

EXPOSE 8000

CMD ["uvicorn", "zoom_faust_fx.server.app:app", "--host", "0.0.0.0", "--port", "8000"]
