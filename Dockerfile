FROM python:3.11-alpine
ARG SHARED_SECRET

RUN apk add --no-cache \
    libusb \
    libusb-dev \
    libffi \
    libffi-dev \
    build-base

WORKDIR /app

RUN mkdir -p /config

COPY . /app/

RUN mkdir -p /app/.internal && \
    echo "$SHARED_SECRET" > /app/.internal/secret.bin && \
    chmod 400 /app/.internal/secret.bin

RUN pip install --no-cache-dir \
        requests \
        pyusb \
        paho-mqtt

ENV PYTHONUNBUFFERED=1 \
    MUTE_CLIENT_CFG=/app/client_config.json

CMD ["python", "/app/client.py"]
