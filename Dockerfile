FROM python:3.13-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home cinema \
    && mkdir -p /app /media /state \
    && chown cinema:cinema /media /state

WORKDIR /app
COPY stream_service.py configuration.py playback.py LICENSE ./
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LISTEN_HOST=0.0.0.0 STREAM_PORT=5057 MEDIA_ROOT=/media STATE_ROOT=/state
USER cinema
EXPOSE 5057
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5057/health', timeout=3).read()"
ENTRYPOINT ["python", "stream_service.py"]
