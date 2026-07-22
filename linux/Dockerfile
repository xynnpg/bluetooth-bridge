FROM python:3.12-slim

# Install Bluetooth, DBus, kernel headers, and C compiler (needed to build python-evdev)
RUN apt-get update && apt-get install -y --no-install-recommends \
    bluez \
    dbus \
    linux-libc-dev \
    build-essential \
    python3-dev \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Install Python packages
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY src/ /app/src/
WORKDIR /app

CMD ["python", "-m", "src.main"]