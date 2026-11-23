#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Замер сквозной задержки аудиотракта «вход → обработка → Icecast».

Метод: два одинаковых ffmpeg-захвата пишутся ОДНОВРЕМЕННО — один читает
источник (вход), второй — выходной маунт Icecast (выход). Затем кросс-
корреляцией находится сдвиг выхода относительно входа. Оба захвата имеют
одинаковый пайплайн и опции минимальной буферизации, поэтому их собственные
задержки взаимно сокращаются (остаточная погрешность ~0,1–0,3 с).

Запуск:
    python tools/measure_latency.py --input <url входа> --output <url выхода> \
        --duration 40 --label "обработка"
"""

import argparse
import os
import shutil
import subprocess
import sys
import threading
import wave

import numpy as np

RATE = 16000  # Гц, моно — достаточно для корреляции речи/музыки

FFMPEG_CANDIDATES = [
    os.environ.get("FFMPEG", ""),
    shutil.which("ffmpeg") or "",
    os.path.join(os.path.dirname(__file__), "..", ".test-venv", "Lib",
                 "site-packages", "imageio_ffmpeg", "binaries",
                 "ffmpeg-win-x86_64-v9.0.2.exe"),
]


def find_ffmpeg():
    for path in FFMPEG_CANDIDATES:
        if path and os.path.exists(path):
            return path
    sys.exit("ffmpeg не найден: задайте FFMPEG=... или положите ffmpeg в PATH")


def capture(url, seconds, wav_path, ffmpeg, result):
    """Один захват: URL -> WAV (16 кГц, моно, s16). Минимальная буферизация."""
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-fflags", "nobuffer", "-flags", "low_delay",
        "-i", url,
        "-t", str(seconds),
        "-ac", "1", "-ar", str(RATE), "-acodec", "pcm_s16le",
        "-y", wav_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=seconds + 60)
    result["rc"] = proc.returncode
    result["stderr"] = proc.stderr


def read_wav(path):
    with wave.open(path, "rb") as fh:
        frames = fh.readframes(fh.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float64) / 32768.0


def measure_delay(sig_in, sig_out, max_lag_s=20.0):
    """Сдвиг sig_out относительно sig_in (сек): полный FFT-коррелятор."""
    n = min(len(sig_in), len(sig_out))
    sig_in, sig_out = sig_in[:n], sig_out[:n]
    # убрать DC и нормировать
    a = sig_in - sig_in.mean()
    b = sig_out - sig_out.mean()
    size = 1 << (2 * n - 1).bit_length()
    fa = np.fft.rfft(a, size)
    fb = np.fft.rfft(b, size)
    corr = np.fft.irfft(fb * np.conj(fa), size)
    # corr[k] = sum b[t] a[t-k] -> пик при k>0: выход ОТСТАЁТ от входа на k.
    # Индексы k >= size/2 — отрицательные лаги (k - size).
    lags = np.arange(size)
    lags[lags > size // 2] -= size
    max_lag = int(max_lag_s * RATE)
    # Отрицательные лаги тоже важны: Icecast при подключении слушателя
    # мгновенно выдаёт очередь (burst) — в записи выход «уезжает вперёд».
    mask = (lags >= -30 * RATE) & (lags <= max_lag)
    idx = np.argmax(np.abs(corr[mask]))
    lag = lags[mask][idx]
    denom = np.sqrt(np.sum(a ** 2) * np.sum(b ** 2))
    coef = corr[lag] / denom if denom else 0.0
    return lag / RATE, coef


def main():
    ap = argparse.ArgumentParser(description="Замер задержки тракта вход→выход")
    ap.add_argument("--input", required=True, help="URL входного потока")
    ap.add_argument("--output", required=True, help="URL выхода (Icecast mount)")
    ap.add_argument("--duration", type=int, default=40, help="длительность записи, с")
    ap.add_argument("--label", default="", help="подпись прогона")
    ap.add_argument("--keep", action="store_true", help="не удалять WAV-файлы (отладка)")
    args = ap.parse_args()

    ffmpeg = find_ffmpeg()
    tmp_in = os.path.join(os.path.dirname(__file__), "_lat_in.wav")
    tmp_out = os.path.join(os.path.dirname(__file__), "_lat_out.wav")

    print("Захват {} с: вход и выход одновременно...".format(args.duration))
    res_in, res_out = {}, {}
    t_in = threading.Thread(target=capture,
                            args=(args.input, args.duration, tmp_in, ffmpeg, res_in))
    t_out = threading.Thread(target=capture,
                             args=(args.output, args.duration, tmp_out, ffmpeg, res_out))
    t_in.start()
    t_out.start()
    t_in.join()
    t_out.join()

    if res_in.get("rc") != 0:
        sys.exit("захват входа не удался: " + (res_in.get("stderr") or "").strip())
    if res_out.get("rc") != 0:
        sys.exit("захват выхода не удался: " + (res_out.get("stderr") or "").strip())

    sig_in, sig_out = read_wav(tmp_in), read_wav(tmp_out)
    delay, coef = measure_delay(sig_in, sig_out)
    label = " ({})".format(args.label) if args.label else ""
    print("Задержка тракта{}: {:.2f} с   (корреляция {:.2f})".format(label, delay, coef))
    if coef < 0.25:
        print("ВНИМАНИЕ: слабая корреляция — оценка ненадёжна "
              "(тишина/музыка без выраженных транзиентов?)")

    for path in (tmp_in, tmp_out):
        rms = float(np.sqrt(np.mean(read_wav(path) ** 2)))
        print("  {}: RMS {:.4f}".format(os.path.basename(path), rms))
    if args.keep:
        print("WAV сохранены: {} {}".format(tmp_in, tmp_out))
        return
    for path in (tmp_in, tmp_out):
        try:
            os.remove(path)
        except OSError:
            pass


if __name__ == "__main__":
    main()
