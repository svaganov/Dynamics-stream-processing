# Панель управления (control plane): Flask + docker SDK + FFmpeg (подслушка A/B).
# Оркестрирует воркеров через Docker API; аудиотракт станций — в воркерах.
# ВАЖНО: контекст сборки — корень репозитория (нужен worker/ для автосборки):
#   docker build -f app/Dockerfile -t audio-panel:2.1 .
FROM python:3.12-slim

WORKDIR /app

# FFmpeg — только для подслушки A/B (MP3-мониторинг в браузер).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/app.py app/liq.py app/entrypoint.py ./
COPY app/static ./static

# Контекст сборки образа воркера — зашит в образ панели, entrypoint
# скармливает его демону tar-потоком, если WORKER_IMAGE не найден.
COPY worker /worker_build

# streams.json и рантайм-скрипты воркеров живут на смонтированном томе,
# чтобы конфиг переживал пересборку образа и был виден воркерам.
ENV HOST=0.0.0.0 \
    PORT=8770 \
    STREAMS_PATH=/data/streams.json \
    WORKER_RUNTIME_DIR=/data/runtime

VOLUME ["/data"]

EXPOSE 8770

CMD ["python", "entrypoint.py"]
