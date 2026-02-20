FROM python:3.12-slim

LABEL org.opencontainers.image.title="aliass"
LABEL org.opencontainers.image.description="Web UI for managing Sonarr and Radarr scene name aliases"
LABEL org.opencontainers.image.source="https://github.com/ramyamounir/aliass"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ templates/

RUN mkdir -p /data/sonarr /data/radarr

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health')" || exit 1

CMD ["gunicorn", "-b", "0.0.0.0:5000", "--workers", "2", "--timeout", "30", "app:app"]
