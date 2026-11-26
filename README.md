# Dynamics Stream Processing

Мультипоточная система обработки аудиопотоков: входной интернет-поток
(AAC/MP3/HLS) → цепочка обработок (AGC, компрессор, лимитер, EQ, стерео,
нормализация) → кодирование → **несколько Icecast-выходов одновременно**
(у каждого свой формат и битрейт).

- Веб-панель управления станциями: http://127.0.0.1:8770
- Подслушка A/B «вход ↔ выход» в браузере
- Контейнерный анализатор сигнала: dBFS, LUFS (EBU R128), True Peak,
  метаданные, полоса пропускания, история громкости
- Проброс StreamTitle (кириллица — UTF-8), автореконнект, резерв при обрыве

## Быстрый старт

```bash
git clone https://github.com/svaganov/Dynamics-stream-processing.git
cd Dynamics-stream-processing
docker compose up -d
docker build -f analyzer/Dockerfile -t audio-analyzer:2.0-panel .
```

Полная документация: **[Инструкция по установке](Инструкция%20по%20установке.md)**
(оба варианта развёртывания, локальный Icecast, перенос настроек,
диагностика проблем).

## Структура

| Каталог | Что внутри |
|---|---|
| `app/` | панель управления (Flask) + её Dockerfile |
| `worker/` | образ воркера обработки (Liquidsoap) |
| `analyzer/v2/` | анализатор уровня/громкости (FastAPI + WebSocket) |
| `tests/` | автотесты (`python -m unittest discover -s tests`) |
| `docker-compose.yml` | контейнерный вариант: панель + оркестрация воркеров |
| `docker-compose.icecast.yml` | локальный icecast-kh для тестов |
