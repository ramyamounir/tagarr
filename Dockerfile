FROM python:3.12-slim

LABEL org.opencontainers.image.title="tagarr"
LABEL org.opencontainers.image.description="Web UI for managing Sonarr and Radarr scene name aliases"
LABEL org.opencontainers.image.source="https://github.com/ramyamounir/tagarr"

RUN apt-get update && apt-get install -y --no-install-recommends gosu && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ templates/
COPY entrypoint.sh /entrypoint.sh

ENV PUID=1000
ENV PGID=1000

RUN mkdir -p /data/sonarr /data/radarr

EXPOSE 5757

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5757/health')" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["gunicorn", "-b", "0.0.0.0:5757", "--workers", "2", "--timeout", "30", "app:app"]
