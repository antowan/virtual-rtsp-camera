# syntax=docker/dockerfile:1
#
# vcam — serve local video files as looping virtual RTSP cameras.
#
# Stages:
#   build   - build the vcam wheel
#   test    - unit test suite      (docker build --target test)
#   runtime - lean serving image   (default; bundles ffmpeg + MediaMTX)

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# Build the wheel
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS build
RUN pip install --no-cache-dir uv
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv build --wheel --out-dir /out

# ---------------------------------------------------------------------------
# Unit tests (no ffmpeg needed - the suite is pure unit tests)
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS test
RUN pip install --no-cache-dir uv
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY tests ./tests
COPY scripts ./scripts
RUN uv sync --frozen --extra prototype
CMD ["uv", "run", "--extra", "prototype", "pytest"]

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# `apt-get upgrade` picks up trixie-security updates published after the base
# image was cut. perl-base is then dropped: nothing in this image needs it
# (ffmpeg and CPython both run fine without it) and it carries CVEs that have
# no fixed package in Debian trixie.
#
# APT_SNAPSHOT exists purely to key this layer. Without it the layer is served
# from the build cache forever, so the upgrade silently freezes at whatever was
# current the day the cache was written and new security updates never land.
# CI passes today's date, so the layer refreshes daily and the slower layers
# below (ffmpeg, the wheel, the MediaMTX download) stay cached.
ARG APT_SNAPSHOT=unpinned
RUN echo "apt snapshot: ${APT_SNAPSHOT}" \
    && apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends ffmpeg \
    && useradd --create-home vcam \
    && apt-get remove -y --allow-remove-essential perl-base \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /vcam

# pip is only needed to install the wheel; dropping it from the runtime image
# removes a package that regularly carries advisories and is never used here.
# Replay (scapy) and synchronized publishing (PyAV) are available in the
# container; the base wheel leaves these opt-in backends out.
COPY --from=build /out/vcam-*.whl /tmp/wheel/
RUN pip install --no-cache-dir "$(ls /tmp/wheel/vcam-*.whl)[replay,sync]" \
    && rm -rf /tmp/wheel \
    && pip uninstall -y pip

# Helper to generate throwaway sample clips inside the image.
COPY scripts/make-sample-videos.sh /opt/vcam/scripts/make-sample-videos.sh
RUN chmod +x /opt/vcam/scripts/make-sample-videos.sh

# Entrypoint wrapper: starts a demo camera when no config is provided.
COPY scripts/entrypoint.sh /opt/vcam/entrypoint.sh
RUN chmod +x /opt/vcam/entrypoint.sh

# Pre-fetch the MediaMTX binary for this image's architecture so `vcam run`
# works offline. Runs as the vcam user, so it caches in /home/vcam/.cache/vcam.
USER vcam
RUN vcam install-server

# RTSP listener + MediaMTX HTTP API (the API is loopback-only).
EXPOSE 8554 9997

# Probes the MediaMTX API of the default server group. If you override
# `api_port` in your config, adjust or drop the healthcheck.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9997/v3/info', timeout=3).status == 200 else 1)"

ENTRYPOINT ["/opt/vcam/entrypoint.sh"]
CMD ["run"]
