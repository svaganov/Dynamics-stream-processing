# Панель управления (control plane): Flask + docker SDK.
# Оркестрирует воркеров через Docker API; аудио внутри панели не обрабатывается.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py liq.py ./
COPY static ./static

# streams.json и рантайм-скрипты воркеров живут на смонтированном томе,
# чтобы (а) конфиг переживал пересборку образа и (б) демон Docker видел
# пути bind-mount'ов воркеров.
ENV HOST=0.0.0.0 \
    PORT=8770 \
    STREAMS_PATH=/data/streams.json \
    WORKER_RUNTIME_DIR=/data/runtime

VOLUME ["/data"]

EXPOSE 8770

CMD ["python", "app.py"]
