FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLATFORMIO_CORE_DIR=/opt/platformio

WORKDIR /app

# System packages for OpenCV, dlib, USB serial tools, and PlatformIO
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl build-essential cmake libgl1 libglib2.0-0 usbutils ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python server + Control Center + PlatformIO dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt zeroconf psutil onnxruntime platformio

# Pre-install ESP32 PlatformIO platform & toolchains into /opt/platformio (no prebuilt .bin in repo)
COPY firmware/esp32-cam/platformio.ini /tmp/pio-prep/platformio.ini
RUN mkdir -p /tmp/pio-prep/src && \
    echo "void setup(){} void loop(){}" > /tmp/pio-prep/src/main.cpp && \
    pio pkg install -d /tmp/pio-prep -e esp32cam && \
    pio pkg install -d /tmp/pio-prep -e esp32wroom && \
    rm -rf /tmp/pio-prep

# Copy application source
COPY . /app
RUN chmod +x /app/scripts/docker-entrypoint.sh

EXPOSE 5000 5001

HEALTHCHECK --interval=15s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsS http://127.0.0.1:5000/api/v1/system/healthz && curl -fsS http://127.0.0.1:5001/api/cc/healthz || exit 1

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
