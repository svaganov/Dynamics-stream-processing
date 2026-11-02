# Воркер обработки: Liquidsoap + сгенерированный панелью скрипт станции.
# Панель монтирует station.liq в /station.liq при создании контейнера.
FROM savonet/liquidsoap:v2.4.2

LABEL org.opencontainers.image.title="audio-worker" \
      org.opencontainers.image.description="Liquidsoap worker for AudioPanel stations"

ENTRYPOINT ["/usr/bin/liquidsoap"]
CMD ["/station.liq"]
