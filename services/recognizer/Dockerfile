# Build context: repo root
# docker build -f services/recognizer/Dockerfile -t openring-recognizer .
FROM docker.io/library/python:3.11-slim@sha256:233de06753d30d120b1a3ce359d8d3be8bda78524cd8f520c99883bfe33964cf

WORKDIR /app

# face_recognition wraps dlib, which needs cmake + a C++ toolchain to
# build the native extension. We pull build deps, install pip deps,
# then strip the toolchain back out to keep the runtime image tight.
# libgl/libglib are pulled in by Pillow + the dlib face-detector.
RUN apt-get update && apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
        gosu \
        cmake \
        build-essential \
        libgl1 \
        libglib2.0-0 \
        libjpeg62-turbo \
        libpng16-16 && \
    rm -rf /var/lib/apt/lists/*

COPY services/recognizer/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# Strip build toolchain after dlib is compiled to shave the image down.
RUN apt-get update && \
    apt-get purge -y cmake build-essential && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

COPY services/recognizer/src/ ./src/
COPY shared/ ./shared/

ENV PYTHONPATH=/app/src:/app/shared
ENV PYTHONUNBUFFERED=1

RUN useradd -r -s /usr/sbin/nologin openring && \
    mkdir -p /data /config /var/lib/openring && \
    chown openring:openring /data /config /var/lib/openring

COPY services/recognizer/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "src/main.py"]
