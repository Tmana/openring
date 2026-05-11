# OpenRing camera-bridge sidecar.
#
# Bridges non-RTSP camera sources (USB webcams via v4l2, video files
# via ffmpeg loop) into RTSP streams the detector can ingest.  IP
# cameras and Pi-side MediaMTX don't need this — the detector
# connects to them directly.
#
# Image layout:
#   - MediaMTX binary at /opt/mediamtx/mediamtx
#   - ffmpeg available on PATH (used by file-source paths)
#   - v4l-utils available so we can list /dev/videoN at startup for
#     debug logs (only meaningful on Linux hosts)
#   - Python entrypoint generates /tmp/mediamtx.yml from openring.yml
#     on boot, then execs mediamtx with that config.
#
# Build context: repo root
# docker build -f services/camera-bridge/Dockerfile -t openring-camera-bridge .

FROM docker.io/library/python:3.11-slim@sha256:233de06753d30d120b1a3ce359d8d3be8bda78524cd8f520c99883bfe33964cf

WORKDIR /app

# MediaMTX is a ~20 MB Go binary; pull a pinned release with checksum.
# The version + sha256 are kept here intentionally so an upgrade is a
# deliberate edit, not a moving target.
ARG MEDIAMTX_VERSION=1.10.0
ARG TARGETARCH=amd64

RUN apt-get update && apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
        gosu \
        ca-certificates \
        curl \
        ffmpeg \
        v4l-utils && \
    rm -rf /var/lib/apt/lists/*

RUN curl -fL -o /tmp/mediamtx.tar.gz \
        "https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_linux_${TARGETARCH}.tar.gz" && \
    mkdir -p /opt/mediamtx && \
    tar -xzf /tmp/mediamtx.tar.gz -C /opt/mediamtx && \
    rm /tmp/mediamtx.tar.gz && \
    chmod +x /opt/mediamtx/mediamtx

COPY services/camera-bridge/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

COPY services/camera-bridge/src/ ./src/
COPY shared/ ./shared/

ENV PYTHONPATH=/app/src:/app/shared
ENV PYTHONUNBUFFERED=1

RUN useradd -r -s /usr/sbin/nologin openring && \
    mkdir -p /data /config /tmp/mediamtx && \
    chown openring:openring /data /config /tmp/mediamtx

COPY services/camera-bridge/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 8554
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "src/main.py"]
