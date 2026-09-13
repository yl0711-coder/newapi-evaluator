FROM python:3.13-slim

ARG RELAY_LAB_IMAGE_REVISION=unknown
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RELAY_LAB_CONTAINER=1 \
    RELAY_LAB_IMAGE_REVISION=${RELAY_LAB_IMAGE_REVISION}

LABEL org.opencontainers.image.title="Relay Station Capacity Lab" \
      org.opencontainers.image.revision=${RELAY_LAB_IMAGE_REVISION}

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt \
    && useradd --create-home --uid 10001 relay-lab \
    && mkdir /data \
    && chown relay-lab:relay-lab /data
COPY relay_lab/ ./relay_lab/

USER relay-lab
EXPOSE 8878
HEALTHCHECK --interval=5s --timeout=2s --start-period=5s --retries=12 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8878/api/state', timeout=1).read()"]
CMD ["python", "-B", "-m", "relay_lab", "ui", "--port", "8878"]
