"""Общий харнес медиапесочниц: фейковый HTTP-источник с ICY-метаданными,
фейковый Icecast, помощники для генерации тестового звука.

(Раньше здесь же жила песочница Этапа 1 для FFmpeg-движка; движок удалён —
единственный тракт станций теперь Liquidsoap, см. sandbox_liquidsoap.py.)
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = PROJECT_ROOT / "app" / "app.py"

spec = importlib.util.spec_from_file_location("panel_app", APP_PATH)
panel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(panel)

try:
    import imageio_ffmpeg
except ImportError:  # optional test dependency
    imageio_ffmpeg = None


BITRATES_MPEG1_L3 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
BITRATES_MPEG2_L3 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
SAMPLE_RATES = {
    3: [44100, 48000, 32000],
    2: [22050, 24000, 16000],
    0: [11025, 12000, 8000],
}


def mp3_frame_length(data, offset):
    if offset + 4 > len(data):
        return None
    h = int.from_bytes(data[offset:offset + 4], "big")
    if (h & 0xFFE00000) != 0xFFE00000:
        return None
    version = (h >> 19) & 0x3
    layer = (h >> 17) & 0x3
    bitrate_idx = (h >> 12) & 0xF
    sample_idx = (h >> 10) & 0x3
    padding = (h >> 9) & 0x1
    if version == 1 or layer != 1 or bitrate_idx in (0, 15) or sample_idx == 3:
        return None
    bitrate = (BITRATES_MPEG1_L3 if version == 3 else BITRATES_MPEG2_L3)[bitrate_idx] * 1000
    sample_rate = SAMPLE_RATES[version][sample_idx]
    coefficient = 144 if version == 3 else 72
    return (coefficient * bitrate) // sample_rate + padding


def add_icy_metadata(mp3_bytes, title, metaint=8192):
    """Встраивает ICY-метаданные на границах MP3-фреймов.

    Возвращает (тело потока, фактический icy-metaint). Интервал должен быть
    кратен длине аудиоблока, иначе FFmpeg будет отрезать metadata по своему
    счётчику и сломает декодирование.
    """
    payload = ("StreamTitle='" + title + "';").encode("utf-8")
    block_len = (len(payload) + 15) // 16
    metadata = bytes([block_len]) + payload.ljust(block_len * 16, b"\x00")

    frames = []
    pos = 0
    while pos < len(mp3_bytes):
        frame_len = mp3_frame_length(mp3_bytes, pos)
        if frame_len is None:
            pos += 1
            continue
        frames.append(mp3_bytes[pos:pos + frame_len])
        pos += frame_len

    if not frames:
        return b"", metaint

    block_frames = 0
    block_audio = 0
    while block_frames < len(frames) and block_audio < metaint:
        block_audio += len(frames[block_frames])
        block_frames += 1

    if block_frames < len(frames):
        # Отрезаем хвост до целого числа ICY-периодов: тело крутится по кругу,
        # и на шве цикла парсер должен попадать ровно на границу периода,
        # иначе после рестарта тела счётчик метаданных разъезжается.
        frames = frames[: (len(frames) // block_frames) * block_frames]

    out = bytearray()
    for i, frame in enumerate(frames):
        out.extend(frame)
        if (i + 1) % block_frames == 0:
            out.extend(metadata)
    return bytes(out), block_audio


class SourceHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not self.path.startswith("/source.mp3"):
            self.send_error(404)
            return
        # Как настоящий Icecast: метаданные в потоке только если клиент
        # явно запросил их заголовком Icy-MetaData: 1.
        wants_icy = self.headers.get("Icy-MetaData") == "1"
        body = self.server.source_body if wants_icy else self.server.raw_body
        self.send_response(200)
        # Server-заголовок настоящего Icecast: Liquidsoap по нему включает
        # self_sync и тянет поток в реальном времени, а не на полной скорости.
        self.send_header("Server", "Icecast 2.4.4")
        self.send_header("Content-Type", "audio/mpeg")
        if wants_icy:
            self.send_header("icy-metaint", str(self.server.metaint))
            self.send_header("icy-name", "Sandbox Source")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        # Крутим тело по кругу в темпе близком к реальному времени (throttle,
        # байт/с): это снимает артефакт зацикливания — декодер не должен
        # проходить шов тела десятки раз за тест, как у живого Icecast.
        throttle = getattr(self.server, "throttle", None)
        try:
            if not throttle:
                while True:
                    self.wfile.write(body)
                    self.wfile.flush()
            else:
                chunk = 4096
                delay = chunk / float(throttle)
                while True:
                    for off in range(0, len(body), chunk):
                        self.wfile.write(body[off:off + chunk])
                        self.wfile.flush()
                        time.sleep(delay)
        except OSError:
            pass

    def log_message(self, fmt, *args):
        pass


class FakeIcecastHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_PUT(self):
        self._consume_stream()

    def do_POST(self):
        self._consume_stream()

    def do_SOURCE(self):
        # Клиенты Icecast (liquidsoap/cry, ices) пушат методом SOURCE
        self._consume_stream()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/admin/metadata":
            self.send_error(404)
            return
        params = parse_qs(parsed.query)
        if params.get("mode", [""])[0] != "updinfo":
            self.send_error(400)
            return
        mount = params.get("mount", [""])[0]
        song = params.get("song", [""])[0]
        self.server.metadata_updates[mount] = song
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def _consume_stream(self):
        mount = self.path.split("?", 1)[0]
        self.server.headers[mount] = {k.lower(): v for k, v in self.headers.items()}
        self.server.bodies.setdefault(mount, bytearray())
        self.send_response(200)
        self.end_headers()

        transfer_encoding = self.headers.get("Transfer-Encoding", "").lower()
        try:
            if transfer_encoding == "chunked":
                while True:
                    size_line = self.rfile.readline(128).strip()
                    if not size_line:
                        break
                    size = int(size_line.split(b";", 1)[0], 16)
                    if size == 0:
                        self.rfile.readline(128)
                        break
                    chunk = self.rfile.read(size)
                    self.rfile.readline(128)
                    self.server.bodies[mount].extend(chunk)
            elif self.headers.get("Content-Length"):
                remaining = int(self.headers["Content-Length"])
                while remaining:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.server.bodies[mount].extend(chunk)
                    remaining -= len(chunk)
            else:
                while True:
                    # Для бесконечного потока без Content-Length read() ждёт полный
                    # буфер; read1() возвращает уже доступный фрагмент.
                    chunk = self.rfile.read1(65536)
                    if not chunk:
                        break
                    self.server.bodies[mount].extend(chunk)
        except (OSError, ValueError):
            pass

    def log_message(self, fmt, *args):
        pass


class SandboxServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, **kwargs):
        super().__init__(address, handler)
        for key, value in kwargs.items():
            setattr(self, key, value)
