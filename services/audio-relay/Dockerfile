# Build context: repo root
# docker build -f services/audio-relay/Dockerfile -t openring-audio-relay .
FROM docker.io/library/python:3.11-slim@sha256:233de06753d30d120b1a3ce359d8d3be8bda78524cd8f520c99883bfe33964cf

WORKDIR /app

RUN apt-get update && apt-get upgrade -y && \
    apt-get install -y --no-install-recommends gosu && \
    rm -rf /var/lib/apt/lists/*

COPY services/audio-relay/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

COPY services/audio-relay/src/ ./src/
COPY shared/ ./shared/

ENV PYTHONPATH=/app/src:/app/shared
ENV PYTHONUNBUFFERED=1

RUN useradd -r -s /usr/sbin/nologin openring && \
    mkdir -p /data /config && \
    chown openring:openring /data /config

COPY services/audio-relay/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "src/main.py"]

EXPOSE 8765
