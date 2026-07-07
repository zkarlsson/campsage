FROM python:3.12-slim

# supercronic: containerized cron (proper signal handling, logs to stdout).
# TARGETARCH is set by BuildKit; default covers plain `docker build` on x86.
ARG TARGETARCH=amd64
ARG SUPERCRONIC_VERSION=v0.2.47
ARG SUPERCRONIC_SHA256_amd64=dcb1403c188a9438c47d4bba82a9c357fc9351ce91627fb2bae627f0f5becfc4
ARG SUPERCRONIC_SHA256_arm64=e1124aa34294e2bb8ab7002f347f4363ba35097f3daf4d3c44e9d813c1fb2bb8

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSLo /usr/local/bin/supercronic \
       "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH}" \
    && sha=$(eval echo "\$SUPERCRONIC_SHA256_${TARGETARCH}") \
    && echo "${sha}  /usr/local/bin/supercronic" | sha256sum -c - \
    && chmod +x /usr/local/bin/supercronic

ENV TZ=America/Los_Angeles \
    CAMPSAGE_DATA_DIR=/data \
    CAMPSAGE_CONTAINER=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x scan-entrypoint.sh

VOLUME /data
EXPOSE 5001

# Default role: web. The scanner service overrides with ./scan-entrypoint.sh
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5001", "--access-logfile", "-", "campsage_web:app"]
