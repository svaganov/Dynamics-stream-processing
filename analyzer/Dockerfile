# Образ анализатора уровня и громкости (dBFS / LUFS EBU R128 / спектр).
#
# Самодостаточный: исходники лежат в analyzer/v2 (копия проекта
# Media-stream-analyzer v2 со встроенным автоподключением по ?url=),
# внешние репозитории для сборки не нужны.
#
# Сборка из корня репозитория:
#   docker build -f analyzer/Dockerfile -t audio-analyzer:2.0-panel .
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# FFmpeg нужен конвейеру анализатора для декодирования потока в PCM
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY analyzer/requirements.txt /tmp/analyzer-requirements.txt
RUN pip install --no-cache-dir -r /tmp/analyzer-requirements.txt

COPY analyzer/v2/__init__.py /app/v2/__init__.py
COPY analyzer/v2/src /app/v2/src

EXPOSE 8765

CMD ["python", "-m", "v2.src.main"]
