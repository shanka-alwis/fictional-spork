FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates tzdata \
 && curl -fsSL https://rclone.org/install.sh | bash \
 && apt-get purge -y unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV DVR_DATA_DIR=/data \
    RCLONE_CONFIG=/data/rclone/rclone.conf \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8090
CMD ["python", "-m", "app.main"]
