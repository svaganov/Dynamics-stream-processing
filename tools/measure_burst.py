#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Замер глубины burst-очереди сервера при подключении слушателя.

Icecast-подобные серверы при подключении мгновенно высылают очередь
(burst-on-connect): первые Q секунд аудио прилетают быстрее реального
времени. Через -progress ffmpeg видно темп обработки: когда он стабилизируется
на 1x, накопленное опережение (audio_time - wall_time) = глубина очереди Q.

Используется вместе с measure_latency.py:
    D = lag(корреляция) - Q_выхода + Q_входа

Запуск: python tools/measure_burst.py <url> [секунд]
"""

import subprocess
import sys
import time

from measure_latency import find_ffmpeg


def measure_burst(url, seconds=25):
    ffmpeg = find_ffmpeg()
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-nostats", "-progress", "pipe:1",
        "-i", url, "-t", str(seconds),
        "-ac", "1", "-ar", "16000", "-f", "null", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    t0 = time.monotonic()
    q_samples = []
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_us="):
            try:
                audio_s = int(line.split("=", 1)[1]) / 1e6
            except ValueError:
                continue
            wall_s = time.monotonic() - t0
            q_samples.append((wall_s, audio_s - wall_s))
    proc.wait()
    if len(q_samples) < 5:
        sys.exit("мало данных — поток не читается?")
    # Q по «хвосту» записи (темп уже 1x): медиана последних 20% отсчётов
    tail = [q for _, q in q_samples[int(len(q_samples) * 0.8):]]
    tail.sort()
    q = tail[len(tail) // 2]
    print("  {} -> Q = {:.2f} с (отсчётов: {})".format(url, q, len(q_samples)))
    return q


if __name__ == "__main__":
    url = sys.argv[1]
    seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    measure_burst(url, seconds)
