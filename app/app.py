#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Мультипоточная панель обработки аудио: N независимых FFmpeg-пайплайнов → Icecast.

Панель является control plane: хранит реестр станций (streams.json), управляет
жизненным циклом отдельных процессов FFmpeg, ведёт логи и предоставляет UI.
Падение одной станции не останавливает остальные.

Запуск:
    python app.py            # http://127.0.0.1:8770
    HOST=0.0.0.0 PORT=8770 python app.py
"""

import base64
import copy
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

from flask import Flask, Response, jsonify, request, send_file, stream_with_context

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LEGACY_CONFIG_PATH = os.environ.get("LEGACY_CONFIG_PATH") or os.path.join(BASE_DIR, "config.json")
STREAMS_PATH = os.environ.get("STREAMS_PATH") or os.path.join(BASE_DIR, "streams.json")
INDEX_PATH = os.path.join(BASE_DIR, "static", "index.html")

# Этап 2: воркеры Liquidsoap в Docker-контейнерах. Рантайм-скрипты станций
# лежат в WORKER_RUNTIME_DIR — путь должен быть виден демону Docker
# (в compose это хостовый том, на dev-хосте Windows — обычная папка проекта).
RUNTIME_DIR = os.environ.get("WORKER_RUNTIME_DIR") or os.path.join(BASE_DIR, "runtime")
WORKER_IMAGE = os.environ.get("WORKER_IMAGE", "audio-worker:2.4.2")

import importlib.util as _ilu

_liq_spec = _ilu.spec_from_file_location("panel_liq", os.path.join(BASE_DIR, "liq.py"))
liq = _ilu.module_from_spec(_liq_spec)
_liq_spec.loader.exec_module(liq)

# --------------------------------------------------------------------------
# Шаблон станции. Движок запуска не привязан к этой модели: на этапе 2
# с тем же реестром станций сможет работать Liquidsoap-воркер.
# --------------------------------------------------------------------------

DEFAULT_STREAM_CONFIG = {
    "id": "station-1",
    "name": "Станция 1",
    "enabled": True,
    "bypass": False,        # True = обход всей обработки (decode -> encode без фильтров)
    "engine": "ffmpeg",     # ffmpeg (процесс) | liquidsoap (Docker-воркер)
    "source": {
        "url": "http://127.0.0.1:8000/input",
        "reconnect": True,
        "reconnect_delay_max": 5,
    },
    "equalizer": {
        "enabled": False,
        # усиление полос в dB, частоты — EQ_FREQS ниже
        "gains": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    },
    "gain": {
        "enabled": False,   # общее усиление после EQ, до компрессора (дБ)
        "db": 0.0,
    },
    "compressor": {
        "enabled": True,
        "threshold": -20,   # dB
        "ratio": 4,
        "attack": 5,        # ms
        "release": 120,     # ms
        "makeup": 6,        # dB
    },
    "loudnorm": {
        "enabled": True,
        "i": -14,           # LUFS
        "tp": -1.5,         # dBTP
        "lra": 11,
    },
    "limiter": {
        "enabled": True,
        "ceiling_db": -1.0, # dBTP
        "attack": 5,        # ms
        "release": 50,      # ms
    },
    "encoder": {
        "codec": "aac",     # aac | libfdk_aac
        "bitrate": 128,     # kbps
        "sample_rate": 44100,
        "channels": 2,
    },
    "icecast": {
        "host": "127.0.0.1",
        "port": 8000,
        "mount": "/station-1",
        "user": "source",
        "password": "hackme",
    },
    "autorestart": {
        "enabled": True,
        "delay": 5,         # сек
    },
}

DEFAULT_SETTINGS = {
    "ffmpeg_path": "",      # пусто = искать ffmpeg в PATH; общий для всех станций
}

STATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

app = Flask(__name__, static_folder="static", static_url_path="/static")

# Частоты полос графического эквалайзера (Гц)
EQ_FREQS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]

# ------------------------------------------------------------- stream store ---

_streams_lock = threading.RLock()


def _deep_merge(base, override):
    """Рекурсивно накатывает override на base (только существующие ключи)."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        elif key in base:
            base[key] = value
    return base


def _default_station(station_id="station-1", name="Станция 1"):
    station = copy.deepcopy(DEFAULT_STREAM_CONFIG)
    station["id"] = station_id
    station["name"] = name
    station["icecast"]["mount"] = "/" + station_id
    return station


def _default_streams_doc():
    return {
        "settings": copy.deepcopy(DEFAULT_SETTINGS),
        "streams": [_default_station()],
    }


def _write_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def _normalize_station(raw, fallback_id=None, fallback_name=None):
    """Восстанавливает полную модель станции и бережно чинит повреждённые поля."""
    station = _default_station()
    if isinstance(raw, dict):
        _deep_merge(station, raw)

    station_id = str(station.get("id") or fallback_id or "station-1").strip()
    if not STATION_ID_RE.match(station_id):
        station_id = fallback_id or "station-1"
    station["id"] = station_id
    station["name"] = str(station.get("name") or fallback_name or station_id).strip() or station_id
    station["enabled"] = bool(station.get("enabled", True))

    engine = str(station.get("engine") or "ffmpeg").strip().lower()
    if engine not in ("ffmpeg", "liquidsoap"):
        engine = "ffmpeg"
    station["engine"] = engine

    gain = station.get("gain") if isinstance(station.get("gain"), dict) else {}
    station["gain"] = {
        "enabled": bool(gain.get("enabled", False)),
        "db": max(-24.0, min(24.0, float(gain.get("db", 0.0) or 0.0))),
    }

    mount = str(station["icecast"].get("mount") or ("/" + station_id)).strip()
    station["icecast"]["mount"] = "/" + mount.lstrip("/")

    gains = station["equalizer"].get("gains")
    if not isinstance(gains, list):
        gains = []
    gains = [float(g) for g in gains[:len(EQ_FREQS)]]
    gains += [0.0] * (len(EQ_FREQS) - len(gains))
    station["equalizer"]["gains"] = [max(-12.0, min(12.0, g)) for g in gains]
    return station


def _normalize_streams_doc(raw):
    doc = _default_streams_doc()
    if isinstance(raw, dict):
        settings = raw.get("settings")
        if isinstance(settings, dict):
            _deep_merge(doc["settings"], settings)

        streams = raw.get("streams")
        if isinstance(streams, list):
            doc["streams"] = []
            for i, item in enumerate(streams):
                fallback_id = "station-{}".format(i + 1)
                doc["streams"].append(
                    _normalize_station(item, fallback_id=fallback_id, fallback_name="Станция {}".format(i + 1))
                )

    if not doc["streams"]:
        doc["streams"].append(_default_station())

    # Загрузка должна быть отказоустойчивой: поврежденный реестр чиним, а не падаем.
    used_ids = set()
    used_mounts = set()
    for station in doc["streams"]:
        sid = station["id"]
        n = 2
        while sid in used_ids:
            sid = "{}-{}".format(station["id"], n)
            n += 1
        station["id"] = sid
        used_ids.add(sid)

        mount = station["icecast"]["mount"]
        n = 2
        while mount in used_mounts:
            mount = "{}-{}".format(station["icecast"]["mount"], n)
            n += 1
        station["icecast"]["mount"] = mount
        used_mounts.add(mount)

    return doc


def _load_streams_unlocked():
    if os.path.exists(STREAMS_PATH):
        try:
            return _normalize_streams_doc(_read_json(STREAMS_PATH))
        except (OSError, ValueError):
            pass

    # Однократная миграция одиночного config.json в station-1.
    doc = _default_streams_doc()
    if os.path.exists(LEGACY_CONFIG_PATH):
        try:
            legacy = _read_json(LEGACY_CONFIG_PATH)
            doc["settings"]["ffmpeg_path"] = str(legacy.get("ffmpeg_path") or "")
            legacy_station = {k: v for k, v in legacy.items() if k != "ffmpeg_path"}
            doc["streams"] = [_normalize_station(legacy_station, fallback_id="station-1", fallback_name="Станция 1")]
        except (OSError, ValueError):
            pass

    try:
        _write_json_atomic(STREAMS_PATH, doc)
    except OSError:
        # Панель всё равно может работать в памяти, даже если файл недоступен.
        pass
    return doc


def _save_streams_unlocked(doc):
    normalized = _normalize_streams_doc(doc)
    _write_json_atomic(STREAMS_PATH, normalized)
    return normalized


def _validate_station_identity(station, current_id=None):
    sid = station["id"]
    if not STATION_ID_RE.match(sid):
        raise ValueError("id станции может содержать латиницу, цифры, '-' и '_'")

    doc = _load_streams_unlocked()
    for other in doc["streams"]:
        if current_id is not None and other["id"] == current_id:
            continue
        if other["id"] == sid:
            raise ValueError("станция с таким id уже существует")
        if other["icecast"]["mount"] == station["icecast"]["mount"]:
            raise ValueError("точка монтирования Icecast уже используется другой станцией")


def load_streams():
    with _streams_lock:
        return _load_streams_unlocked()


def save_streams(doc):
    with _streams_lock:
        return _save_streams_unlocked(doc)


def list_stations():
    return load_streams()["streams"]


def get_station(station_id):
    for station in list_stations():
        if station["id"] == station_id:
            return station
    return None


def first_station_id():
    stations = list_stations()
    return stations[0]["id"] if stations else None


def create_station(raw):
    if not isinstance(raw, dict):
        raise ValueError("некорректная станция")
    with _streams_lock:
        doc = _load_streams_unlocked()
        if not str(raw.get("id") or "").strip():
            n = len(doc["streams"]) + 1
            candidate = "station-{}".format(n)
            ids = {s["id"] for s in doc["streams"]}
            while candidate in ids:
                n += 1
                candidate = "station-{}".format(n)
            raw = dict(raw)
            raw["id"] = candidate
        station = _normalize_station(raw, fallback_name=raw.get("name") or raw.get("id"))
        # У новой станции без явного mount используем собственный id, а mount шаблона.
        raw_icecast = raw.get("icecast") if isinstance(raw.get("icecast"), dict) else {}
        if "mount" not in raw_icecast:
            station["icecast"]["mount"] = "/" + station["id"]
        _validate_station_identity(station)
        doc["streams"].append(station)
        _save_streams_unlocked(doc)
        return copy.deepcopy(station)


def update_station(station_id, raw):
    if not isinstance(raw, dict):
        raise ValueError("некорректная станция")
    with _streams_lock:
        doc = _load_streams_unlocked()
        for i, station in enumerate(doc["streams"]):
            if station["id"] != station_id:
                continue
            merged = copy.deepcopy(station)
            _deep_merge(merged, {k: v for k, v in raw.items() if k != "id"})
            merged = _normalize_station(merged, fallback_id=station_id, fallback_name=station["name"])
            merged["id"] = station_id
            _validate_station_identity(merged, current_id=station_id)
            doc["streams"][i] = merged
            _save_streams_unlocked(doc)
            return copy.deepcopy(merged)
    raise KeyError(station_id)


def delete_station(station_id):
    with _streams_lock:
        doc = _load_streams_unlocked()
        original = len(doc["streams"])
        if original <= 1:
            raise ValueError("нельзя удалить последнюю станцию")
        doc["streams"] = [s for s in doc["streams"] if s["id"] != station_id]
        if len(doc["streams"]) == original:
            raise KeyError(station_id)
        _save_streams_unlocked(doc)


def update_settings(raw):
    if not isinstance(raw, dict):
        raise ValueError("некорректные настройки")
    with _streams_lock:
        doc = _load_streams_unlocked()
        _deep_merge(doc["settings"], {k: v for k, v in raw.items() if k in doc["settings"]})
        _save_streams_unlocked(doc)
        return copy.deepcopy(doc["settings"])


def load_config():
    """Плоский конфиг первой станции — обратная совместимость старого UI/API."""
    with _streams_lock:
        doc = _load_streams_unlocked()
        if not doc["streams"]:
            return None
        cfg = copy.deepcopy(doc["streams"][0])
        cfg["ffmpeg_path"] = doc["settings"].get("ffmpeg_path", "")
        return cfg


def save_config(cfg):
    """Сохраняет плоский старый конфиг в первую станцию и общие настройки."""
    if not isinstance(cfg, dict):
        raise ValueError("некорректный конфиг")
    with _streams_lock:
        doc = _load_streams_unlocked()
        if not doc["streams"]:
            doc["streams"].append(_default_station())
        station_update = {k: v for k, v in cfg.items() if k != "ffmpeg_path"}
        merged = copy.deepcopy(doc["streams"][0])
        _deep_merge(merged, station_update)
        doc["streams"][0] = _normalize_station(merged, fallback_id=doc["streams"][0]["id"], fallback_name=doc["streams"][0]["name"])
        doc["settings"]["ffmpeg_path"] = str(cfg.get("ffmpeg_path") or "")
        _save_streams_unlocked(doc)
        return load_config()


# --------------------------------------------------------------- command ---


def build_command(cfg):
    """Собирает argv FFmpeg из плоского runtime-конфига одной станции."""
    filters = []

    if not cfg.get("bypass"):
        # Эквалайзер — первым в цепочке: сначала спектр, потом динамика.
        # Полосы с нулевым усилением пропускаем, чтобы не плодить фильтры.
        eq = cfg["equalizer"]
        if eq.get("enabled"):
            for freq, gain in zip(EQ_FREQS, eq.get("gains", [])):
                if abs(float(gain)) > 0.01:
                    filters.append("equalizer=f={}:t=o:w=1:g={}".format(freq, gain))

        # Статическое усиление — после EQ, до компрессора (та же точка цепочки,
        # что и ffmpeg.filter.volume в воркере Liquidsoap).
        gain = cfg.get("gain") or {}
        if gain.get("enabled"):
            db = float(gain.get("db", 0.0) or 0.0)
            if abs(db) > 0.01:
                filters.append("volume={:g}dB".format(db))

        comp = cfg["compressor"]
        if comp.get("enabled"):
            filters.append(
                "acompressor=threshold={t}dB:ratio={r}:attack={a}:release={rel}:makeup={m}dB".format(
                    t=comp["threshold"], r=comp["ratio"],
                    a=comp["attack"], rel=comp["release"], m=comp["makeup"],
                )
            )

        ln = cfg["loudnorm"]
        if ln.get("enabled"):
            filters.append("loudnorm=I={i}:TP={tp}:LRA={lra}".format(
                i=ln["i"], tp=ln["tp"], lra=ln["lra"]))

        lim = cfg["limiter"]
        if lim.get("enabled"):
            ceiling = round(10 ** (float(lim["ceiling_db"]) / 20.0), 6)
            filters.append("alimiter=limit={c}:attack={a}:release={r}".format(
                c=ceiling, a=lim["attack"], r=lim["release"]))

    ffmpeg_bin = (cfg.get("ffmpeg_path") or "").strip() or "ffmpeg"

    cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "info", "-stats_period", "2"]

    src = cfg["source"]
    if src.get("reconnect"):
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                "-reconnect_delay_max", str(src.get("reconnect_delay_max", 5))]

    cmd += ["-i", src["url"]]

    if filters:
        cmd += ["-af", ",".join(filters)]

    enc = cfg["encoder"]
    cmd += ["-vn", "-c:a", enc["codec"], "-b:a", "{}k".format(enc["bitrate"]),
            "-ar", str(enc["sample_rate"]), "-ac", str(enc["channels"])]

    ice = cfg["icecast"]
    ice_url = "icecast://{user}:{pw}@{host}:{port}/{mount}".format(
        user=ice["user"], pw=ice["password"],
        host=ice["host"], port=ice["port"],
        mount=str(ice["mount"]).lstrip("/"),
    )
    cmd += ["-f", "adts", ice_url]
    return cmd


_ICECAST_URL_RE = re.compile(r"(icecast://[^:/\s]+):[^@\s]+@")


def _mask_line(line):
    """Маскирует пароль в URL icecast://user:pass@host, которые печатает сам FFmpeg."""
    return _ICECAST_URL_RE.sub(r"\1:***@", line)


def masked_command(cmd):
    """Команда с замаскированным паролем Icecast — для лога и API."""
    return [_mask_line(part) for part in cmd]


def station_runtime_config(station, settings=None):
    """Склеивает станцию с общими настройками в плоский конфиг FFmpeg."""
    cfg = copy.deepcopy(station)
    cfg["ffmpeg_path"] = (settings or {}).get("ffmpeg_path", "")
    return cfg


def find_ffmpeg(cfg):
    """Возвращает (путь | None, версия | None)."""
    binary = (cfg.get("ffmpeg_path") or "").strip() or "ffmpeg"
    resolved = binary if os.path.sep in binary else shutil.which(binary)
    if not resolved or not os.path.exists(resolved):
        return None, None
    try:
        proc = subprocess.run(
            [resolved, "-version"], capture_output=True, text=True, timeout=10,
        )
        first = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
        return resolved, first.strip() or None
    except (OSError, subprocess.SubprocessError):
        return resolved, None


# ------------------------------------------------------------- metadata ---

# FFmpeg корректно встраивает StreamTitle в MP3-выход, но ADTS/AAC в Icecast
# отдаёт без динамических metadata-блоков. Поэтому для AAC-пайплайна title
# читается отдельным лёгким ICY-подключением и публикуется стандартным
# Icecast endpoint /admin/metadata, не затрагивая аудиоданные.
_STREAM_TITLE_RE = re.compile(r"StreamTitle='((?:[^']|'')*)';")


class MetadataRelay:
    def __init__(self, station_id, cfg, logger):
        self.station_id = station_id
        self.cfg = copy.deepcopy(cfg)
        self.logger = logger
        self.stop_event = threading.Event()
        self.thread = None
        self.last_title = None

    def start(self):
        self.stop()
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        thread = self.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        self.thread = None

    def log(self, text):
        self.logger.log("[metadata] {}".format(text))

    def _run(self):
        url = str(self.cfg["source"].get("url") or "").strip()
        response = None
        try:
            request = urllib.request.Request(url, headers={
                "Icy-MetaData": "1",
                "User-Agent": "KimiAudioPanel/1.0",
            })
            response = urllib.request.urlopen(request, timeout=10)
            metaint = int(response.headers.get("icy-metaint") or "0")
            if metaint <= 0:
                self.log("источник не объявил icy-metaint — динамические metadata недоступны")
                return
            self.log("слежу за ICY-метаданными, интервал {} байт".format(metaint))

            while not self.stop_event.is_set():
                try:
                    if not self._skip_audio(response, metaint):
                        self.log("источник закрыл поток")
                        break
                    size_raw = self._read_exact(response, 1)
                    if not size_raw:
                        self.log("источник закрыл поток")
                        break
                    block_size = size_raw[0] * 16
                    block = self._read_exact(response, block_size) if block_size else b""
                    title = self._extract_stream_title(block)
                    if title is not None and title != self.last_title:
                        self.last_title = title
                        self._publish_title(title)
                except (socket.timeout, TimeoutError):
                    continue
                except (OSError, urllib.error.URLError) as exc:
                    if not self.stop_event.is_set():
                        self.log("поток метаданных прерван: {}".format(exc))
                    break
        except (OSError, ValueError, urllib.error.URLError) as exc:
            if not self.stop_event.is_set():
                self.log("не удалось открыть metadata-поток: {}".format(exc))
        finally:
            if response is not None:
                try:
                    response.close()
                except OSError:
                    pass

    @staticmethod
    def _read_exact(response, size):
        chunks = []
        remaining = size
        while remaining > 0:
            chunk = response.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _skip_audio(self, response, size):
        remaining = size
        while remaining > 0:
            chunk = response.read(min(8192, remaining))
            if not chunk:
                return False
            remaining -= len(chunk)
        return True

    @staticmethod
    def _extract_stream_title(block):
        if not block:
            return None
        text = block.decode("utf-8", errors="replace")
        match = _STREAM_TITLE_RE.search(text)
        if not match:
            return None
        return match.group(1).replace("''", "'").strip()

    def _publish_title(self, title):
        ice = self.cfg["icecast"]
        host = str(ice.get("host") or "").strip()
        scheme = "http"
        if host.startswith("http://"):
            host = host[len("http://"):]
        elif host.startswith("https://"):
            scheme = "https"
            host = host[len("https://"):]
        query = urllib.parse.urlencode({
            "mount": str(ice.get("mount") or ""),
            "mode": "updinfo",
            "song": title,
        })
        url = "{}://{}:{}/admin/metadata?{}".format(
            scheme, host, int(ice.get("port", 8000)), query,
        )
        user = str(ice.get("user") or "source")
        password = str(ice.get("password") or "")
        auth = base64.b64encode("{}:{}".format(user, password).encode("utf-8")).decode("ascii")

        try:
            request = urllib.request.Request(url, headers={
                "Authorization": "Basic {}".format(auth),
                "User-Agent": "KimiAudioPanel/1.0",
            })
            with urllib.request.urlopen(request, timeout=5) as response:
                response.read()
            self.log("StreamTitle обновлён на Icecast")
        except urllib.error.HTTPError as exc:
            self.log("Icecast не принял metadata: HTTP {}".format(exc.code))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            self.log("не удалось опубликовать metadata: {}".format(exc))


# --------------------------------------------------------- process manager ---


class ProcessManager:
    def __init__(self, station_id):
        self.station_id = station_id
        self.lock = threading.Lock()
        self.proc = None
        self.manual_stop = True
        self.started_at = None
        self.restarts = 0
        self.last_exit = None
        self.logs = deque(maxlen=3000)
        self.next_log_id = 1
        self._run_cfg = None
        self._ffmpeg_cache = (0.0, None, None)  # (ts, path, version)
        self.metadata_relay = None
        threading.Thread(target=self._watchdog, daemon=True).start()

    # ------------------------------------------------------------- logging

    def log(self, text):
        with self.lock:
            self.logs.append({
                "id": self.next_log_id,
                "ts": time.strftime("%H:%M:%S"),
                "line": text,
            })
            self.next_log_id += 1

    def logs_since(self, since):
        with self.lock:
            items = [entry for entry in self.logs if entry["id"] > since]
            return items, self.next_log_id - 1

    # ------------------------------------------------------------ control

    def _start_metadata_relay(self, cfg):
        self._stop_metadata_relay()
        relay = MetadataRelay(self.station_id, cfg, self)
        self.metadata_relay = relay
        relay.start()

    def _stop_metadata_relay(self):
        relay = self.metadata_relay
        self.metadata_relay = None
        if relay is not None:
            relay.stop()

    def start(self, cfg):
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return False, "процесс уже запущен"
            cmd = build_command(cfg)
            binary = cmd[0]
            resolved = binary if os.path.sep in binary else shutil.which(binary)
            if not resolved or not os.path.exists(resolved):
                return False, "ffmpeg не найден: {}".format(binary)
            cmd[0] = resolved
            self.manual_stop = False
            kwargs = {}
            if sys.platform.startswith("win"):
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                self.proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    **kwargs
                )
            except OSError as exc:
                self.proc = None
                return False, "не удалось запустить: {}".format(exc)
            self._run_cfg = copy.deepcopy(cfg)
            self.started_at = time.time()
            proc = self.proc

        self.log("[panel] запущен FFmpeg, PID {}:".format(proc.pid))
        self.log("[panel]   " + " ".join(masked_command(cmd)))
        threading.Thread(target=self._reader, args=(proc,), daemon=True).start()
        self._start_metadata_relay(cfg)
        return True, "запущен (PID {})".format(proc.pid)

    def stop(self):
        with self.lock:
            self.manual_stop = True
            proc = self.proc
        if proc is None or proc.poll() is not None:
            self._stop_metadata_relay()
            return False, "процесс не запущен"
        self.log("[panel] остановка, PID {}...".format(proc.pid))
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except OSError:
            pass
        self._stop_metadata_relay()
        self.log("[panel] остановлен")
        return True, "остановлен"

    def restart(self, cfg=None):
        was_running = False
        with self.lock:
            was_running = self.proc is not None and self.proc.poll() is None
            next_cfg = copy.deepcopy(cfg if cfg is not None else self._run_cfg)
        if was_running:
            self.stop()
            time.sleep(0.5)
        if next_cfg is None:
            return False, "нет конфигурации станции"
        ok, msg = self.start(next_cfg)
        return ok, ("перезапущен" if ok else msg)

    # -------------------------------------------------------------- status

    def status(self, current_cfg=None):
        with self.lock:
            running = self.proc is not None and self.proc.poll() is None
            pid = self.proc.pid if running else None
            uptime = int(time.time() - self.started_at) if (running and self.started_at) else 0
            restarts = self.restarts
            last_exit = self.last_exit
        path, version = self.ffmpeg_info(current_cfg)
        return {
            "station_id": self.station_id,
            "running": running,
            "pid": pid,
            "uptime": uptime,
            "restarts": restarts,
            "last_exit": last_exit,
            "engine": "ffmpeg",
            "detail": version or "FFmpeg",
            "ffmpeg_found": path is not None,
            "ffmpeg_version": version,
        }

    def ffmpeg_info(self, cfg=None):
        now = time.time()
        binary = ((cfg or {}).get("ffmpeg_path") or "").strip()
        cache_key = binary or "ffmpeg"
        ts, path, version = self._ffmpeg_cache
        if now - ts < 15 and (path == cache_key or path is not None):
            return path, version
        path, version = find_ffmpeg(cfg or {"ffmpeg_path": binary})
        self._ffmpeg_cache = (now, path, version)
        return path, version

    # ------------------------------------------------------------- threads

    def _reader(self, proc):
        try:
            for line in proc.stderr:
                self.log(_mask_line(line.rstrip("\r\n")))
        except (OSError, ValueError):
            pass
        rc = proc.wait()
        with self.lock:
            if self.proc is proc:
                self.last_exit = rc
        self._stop_metadata_relay()
        self.log("[panel] процесс завершился, код {}".format(rc))

    def _watchdog(self):
        while True:
            time.sleep(1)
            with self.lock:
                proc = self.proc
                manual = self.manual_stop
                cfg = copy.deepcopy(self._run_cfg)
            if proc is None or manual or cfg is None or proc.poll() is None:
                continue
            auto = cfg.get("autorestart", {})
            if not auto.get("enabled"):
                continue
            delay = max(1, int(auto.get("delay", 5)))
            self.log("[panel] автоперезапуск через {} с...".format(delay))
            time.sleep(delay)
            with self.lock:
                if self.manual_stop or self.proc is not proc:
                    continue
            ok, msg = self.start(cfg)
            if ok:
                with self.lock:
                    self.restarts += 1
                self.log("[panel] автоперезапуск выполнен")
            else:
                self.log("[panel] автоперезапуск не удался: {}".format(msg))
                time.sleep(5)


class DockerWorkerManager:
    """Менеджер станции на движке Liquidsoap: один Docker-контейнер на станцию.

    Тот же контракт, что у ProcessManager (start/stop/restart/status/logs),
    чтобы маршруты API не замечали разницы движков. Метаданные ICY идут
    сквозь Liquidsoap natively, отдельный MetadataRelay не нужен.
    """

    def __init__(self, station_id):
        self.station_id = station_id
        safe = re.sub(r"[^A-Za-z0-9_.-]", "-", station_id)
        self.container_name = "audio-worker-{}".format(safe)
        self.lock = threading.Lock()
        self.container = None
        self.manual_stop = True
        self.started_at = None
        self.restarts = 0
        self.last_exit = None
        self.logs = deque(maxlen=3000)
        self.next_log_id = 1
        self._run_cfg = None
        self._client = None
        threading.Thread(target=self._watchdog, daemon=True).start()

    # ------------------------------------------------------------- logging

    def log(self, text):
        with self.lock:
            self.logs.append({
                "id": self.next_log_id,
                "ts": time.strftime("%H:%M:%S"),
                "line": text,
            })
            self.next_log_id += 1

    def logs_since(self, since):
        with self.lock:
            items = [entry for entry in self.logs if entry["id"] > since]
            return items, self.next_log_id - 1

    # ------------------------------------------------------------ docker

    def _docker_client(self):
        if self._client is None:
            import docker
            self._client = docker.from_env()
        return self._client

    def _script_path(self):
        runtime_dir = os.path.join(RUNTIME_DIR, re.sub(r"[^A-Za-z0-9_.-]", "-", self.station_id))
        os.makedirs(runtime_dir, exist_ok=True)
        return os.path.join(runtime_dir, "station.liq")

    @staticmethod
    def _is_running(container):
        try:
            container.reload()
            return container.status == "running"
        except Exception:
            return False

    def _find_container(self, client):
        for container in client.containers.list(all=True, filters={"name": self.container_name}):
            if container.name == self.container_name:
                return container
        return None

    # ------------------------------------------------------------ control

    def start(self, cfg):
        with self.lock:
            if self.container is not None and self._is_running(self.container):
                return False, "воркер уже запущен"
        script = liq.generate_liq_script(cfg)
        script_path = self._script_path()
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(script)

        try:
            client = self._docker_client()
        except ImportError:
            return False, "docker SDK не установлен (pip install docker)"
        except Exception as exc:
            return False, "демон Docker недоступен: {}".format(exc)

        try:
            existing = self._find_container(client)
            if existing is not None:
                try:
                    existing.remove(force=True)
                except Exception:
                    pass
            import docker
            container = client.containers.create(
                WORKER_IMAGE,
                name=self.container_name,
                command=["/station.liq"],
                mounts=[docker.types.Mount(
                    target="/station.liq", source=script_path,
                    type="bind", read_only=True,
                )],
                labels={"audio-panel": "1", "audio-panel.station": self.station_id},
                extra_hosts={"host.docker.internal": "host-gateway"},
            )
            container.start()
        except Exception as exc:
            return False, "не удалось запустить воркер: {}".format(exc)

        with self.lock:
            self.container = container
            self.manual_stop = False
            self._run_cfg = copy.deepcopy(cfg)
            self.started_at = time.time()
        self.log("[panel] запущен воркер Liquidsoap, контейнер {}".format(self.container_name))
        for line in liq.mask_liq_script(script, cfg).splitlines():
            if line.strip() and not line.startswith("#"):
                self.log("[panel]   " + line)
        threading.Thread(target=self._follow_logs, args=(container,), daemon=True).start()
        return True, "запущен ({})".format(self.container_name)

    def stop(self):
        with self.lock:
            self.manual_stop = True
            container = self.container
        if container is None or not self._is_running(container):
            return False, "процесс не запущен"
        self.log("[panel] остановка воркера {}...".format(self.container_name))
        try:
            container.stop(timeout=5)
        except Exception:
            try:
                container.kill()
            except Exception:
                pass
        self.log("[panel] остановлен")
        return True, "остановлен"

    def restart(self, cfg=None):
        with self.lock:
            was_running = self.container is not None and self._is_running(self.container)
            next_cfg = copy.deepcopy(cfg if cfg is not None else self._run_cfg)
        if was_running:
            self.stop()
            time.sleep(0.5)
        if next_cfg is None:
            return False, "нет конфигурации станции"
        ok, msg = self.start(next_cfg)
        return ok, ("перезапущен" if ok else msg)

    def dispose(self):
        """Остановка + удаление контейнера (при смене движка или удалении станции)."""
        self.stop()
        with self.lock:
            container = self.container
            self.container = None
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                pass

    # -------------------------------------------------------------- status

    def status(self, current_cfg=None):
        with self.lock:
            container = self.container
            restarts = self.restarts
            last_exit = self.last_exit
        running = False
        pid = None
        uptime = 0
        if container is not None:
            try:
                container.reload()
                running = container.status == "running"
                if running:
                    pid = container.attrs["State"]["Pid"] or None
                    started_at = container.attrs["State"].get("StartedAt") or ""
                    try:
                        from datetime import datetime
                        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                        uptime = max(0, int((datetime.now(started.tzinfo) - started).total_seconds()))
                    except ValueError:
                        uptime = int(time.time() - self.started_at) if self.started_at else 0
                else:
                    pid = None
            except Exception:
                running = False
        return {
            "station_id": self.station_id,
            "running": running,
            "pid": pid,
            "uptime": uptime,
            "restarts": restarts,
            "last_exit": last_exit,
            "engine": "liquidsoap",
            "detail": "Liquidsoap worker · {}".format(WORKER_IMAGE),
            "ffmpeg_found": False,
            "ffmpeg_version": None,
            "container": self.container_name if container is not None else None,
        }

    # ------------------------------------------------------------- threads

    def _follow_logs(self, container):
        try:
            stream = container.logs(stdout=True, stderr=True, stream=True, follow=True)
            for chunk in stream:
                if isinstance(chunk, bytes):
                    chunk = chunk.decode("utf-8", errors="replace")
                for line in chunk.splitlines():
                    if line.strip():
                        self.log(_mask_line(line))
        except Exception:
            pass
        exit_code = None
        try:
            container.reload()
            exit_code = container.attrs["State"].get("ExitCode")
        except Exception:
            pass
        with self.lock:
            if self.container is container:
                self.last_exit = exit_code
        self.log("[panel] воркер завершился, код {}".format(exit_code))

    def _watchdog(self):
        while True:
            time.sleep(1)
            with self.lock:
                container = self.container
                manual = self.manual_stop
                cfg = copy.deepcopy(self._run_cfg)
            if container is None or manual or cfg is None:
                continue
            if self._is_running(container):
                continue
            auto = cfg.get("autorestart", {})
            if not auto.get("enabled"):
                continue
            delay = max(1, int(auto.get("delay", 5)))
            self.log("[panel] автоперезапуск через {} с...".format(delay))
            time.sleep(delay)
            with self.lock:
                if self.manual_stop or self.container is not container:
                    continue
            ok, msg = self.start(cfg)
            if ok:
                with self.lock:
                    self.restarts += 1
                self.log("[panel] автоперезапуск выполнен")
            else:
                self.log("[panel] автоперезапуск не удался: {}".format(msg))
                time.sleep(5)


MANAGER_CLASSES = {
    "ffmpeg": ProcessManager,
    "liquidsoap": DockerWorkerManager,
}


_managers_lock = threading.Lock()
_managers = {}


def get_manager(station_id):
    """Возвращает изолированный менеджер станции под её движок запуска."""
    station = get_station(station_id)
    engine = (station or {}).get("engine") or "ffmpeg"
    cls = MANAGER_CLASSES.get(engine, ProcessManager)
    with _managers_lock:
        manager = _managers.get(station_id)
        if manager is not None and not isinstance(manager, cls):
            # Движок станции сменился — старый менеджер выключаем.
            try:
                manager.dispose()
            except AttributeError:
                manager.stop()
            manager = None
        if manager is None:
            manager = cls(station_id)
            _managers[station_id] = manager
        return manager


def remove_manager(station_id):
    with _managers_lock:
        return _managers.pop(station_id, None)


def runtime_config_for_station(station_id):
    doc = load_streams()
    for station in doc["streams"]:
        if station["id"] == station_id:
            return station_runtime_config(station, doc["settings"])
    raise KeyError(station_id)


def stop_and_forget_station(station_id):
    """Останавливает только процессы удаляемой станции и убирает её менеджер."""
    manager = remove_manager(station_id)
    if manager is not None:
        if hasattr(manager, "dispose"):
            manager.dispose()
        else:
            manager.stop()
    stop_listeners(station_id)


# A/B-прослушки отделены от основного pipeline: они живут, пока клиент слушает
# HTTP-ответ, и принудительно останавливаются при удалении станции.
_listeners_lock = threading.Lock()
_listeners = {}


def register_listener(station_id, proc):
    key = station_id or "__legacy__"
    with _listeners_lock:
        _listeners.setdefault(key, set()).add(proc)


def unregister_listener(station_id, proc):
    key = station_id or "__legacy__"
    with _listeners_lock:
        procs = _listeners.get(key)
        if procs is not None:
            procs.discard(proc)
            if not procs:
                _listeners.pop(key, None)


def stop_listeners(station_id):
    key = station_id or "__legacy__"
    with _listeners_lock:
        procs = list(_listeners.pop(key, set()))
    for proc in procs:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            pass


# ------------------------------------------------------------------- API ---


def _station_not_found():
    return jsonify({"ok": False, "error": "станция не найдена"}), 404


def _station_config_or_404(station_id):
    try:
        return runtime_config_for_station(station_id)
    except KeyError:
        return None


@app.route("/")
def index():
    return send_file(INDEX_PATH)


@app.route("/api/streams", methods=["GET"])
def api_streams_list():
    doc = load_streams()
    return jsonify({
        "ok": True,
        "settings": doc["settings"],
        "streams": doc["streams"],
    })


@app.route("/api/streams", methods=["POST"])
def api_stream_create():
    body = request.get_json(silent=True)
    try:
        station = create_station(body or {})
    except ValueError as exc:
        status = 409 if "уже" in str(exc) else 400
        return jsonify({"ok": False, "error": str(exc)}), status
    return jsonify({"ok": True, "station": station}), 201


@app.route("/api/streams/<station_id>", methods=["GET"])
def api_stream_get(station_id):
    station = get_station(station_id)
    if station is None:
        return _station_not_found()
    return jsonify({"ok": True, "station": station})


@app.route("/api/streams/<station_id>", methods=["PUT"])
def api_stream_update(station_id):
    body = request.get_json(silent=True)
    try:
        station = update_station(station_id, body or {})
    except KeyError:
        return _station_not_found()
    except ValueError as exc:
        status = 409 if "уже" in str(exc) else 400
        return jsonify({"ok": False, "error": str(exc)}), status
    return jsonify({"ok": True, "station": station})


@app.route("/api/streams/<station_id>", methods=["DELETE"])
def api_stream_delete(station_id):
    try:
        stop_and_forget_station(station_id)
        delete_station(station_id)
    except KeyError:
        return _station_not_found()
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["GET", "PUT"])
def api_settings():
    if request.method == "GET":
        return jsonify({"ok": True, "settings": load_streams()["settings"]})
    body = request.get_json(silent=True)
    try:
        settings = update_settings(body or {})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "settings": settings})


def _start_station(station_id):
    cfg = _station_config_or_404(station_id)
    if cfg is None:
        return _station_not_found()
    ok, msg = get_manager(station_id).start(cfg)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)


def _stop_station(station_id):
    if get_station(station_id) is None:
        return _station_not_found()
    ok, msg = get_manager(station_id).stop()
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)


def _restart_station(station_id):
    cfg = _station_config_or_404(station_id)
    if cfg is None:
        return _station_not_found()
    ok, msg = get_manager(station_id).restart(cfg)
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 409)


@app.route("/api/streams/<station_id>/start", methods=["POST"])
def api_station_start(station_id):
    return _start_station(station_id)


@app.route("/api/streams/<station_id>/stop", methods=["POST"])
def api_station_stop(station_id):
    return _stop_station(station_id)


@app.route("/api/streams/<station_id>/restart", methods=["POST"])
def api_station_restart(station_id):
    return _restart_station(station_id)


@app.route("/api/streams/<station_id>/status", methods=["GET"])
def api_station_status(station_id):
    cfg = _station_config_or_404(station_id)
    if cfg is None:
        return _station_not_found()
    return jsonify(get_manager(station_id).status(cfg))


@app.route("/api/streams/<station_id>/logs", methods=["GET"])
def api_station_logs(station_id):
    if get_station(station_id) is None:
        return _station_not_found()
    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        since = 0
    items, latest = get_manager(station_id).logs_since(since)
    return jsonify({"logs": items, "latest": latest})


@app.route("/api/streams/<station_id>/command", methods=["POST"])
def api_station_command(station_id):
    station = get_station(station_id)
    if station is None:
        return _station_not_found()
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        station = _normalize_station(_deep_merge(copy.deepcopy(station), body))
    settings = load_streams()["settings"]
    if station.get("engine") == "liquidsoap":
        script = liq.generate_liq_script(station_runtime_config(station, settings))
        return jsonify({"cmd": [], "text": liq.mask_liq_script(script, station)})
    cmd = build_command(station_runtime_config(station, settings))
    return jsonify({"cmd": masked_command(cmd), "text": " ".join(masked_command(cmd))})


@app.route("/api/health", methods=["GET"])
def api_health():
    doc = load_streams()
    stations = []
    counts = {
        "total": len(doc["streams"]),
        "running": 0,
        "stopped": 0,
    }
    for station in doc["streams"]:
        cfg = station_runtime_config(station, doc["settings"])
        status = get_manager(station["id"]).status(cfg)
        counts["running" if status["running"] else "stopped"] += 1
        stations.append({
            "id": station["id"],
            "name": station["name"],
            "enabled": station["enabled"],
            "engine": station.get("engine") or "ffmpeg",
            "running": status["running"],
            "pid": status["pid"],
            "uptime": status["uptime"],
            "restarts": status["restarts"],
            "last_exit": status["last_exit"],
        })
    return jsonify({"ok": True, "counts": counts, "stations": stations})


# ----------------------------- compatibility API for the single-station UI ---


@app.route("/api/config", methods=["GET"])
def api_get_config():
    cfg = load_config()
    if cfg is None:
        return jsonify({"ok": False, "error": "нет станций"}), 404
    return jsonify(cfg)


@app.route("/api/config", methods=["POST"])
def api_save_config():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "некорректный JSON"}), 400
    try:
        merged = save_config(body)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "config": merged})


@app.route("/api/command", methods=["POST"])
def api_command():
    body = request.get_json(silent=True)
    cfg = load_config()
    if cfg is None:
        return jsonify({"ok": False, "error": "нет станций"}), 404
    if isinstance(body, dict):
        cfg = _deep_merge(copy.deepcopy(cfg), body)
    cmd = build_command(cfg)
    return jsonify({"cmd": masked_command(cmd), "text": " ".join(masked_command(cmd))})


@app.route("/api/start", methods=["POST"])
def api_start():
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        try:
            save_config(body)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    station_id = first_station_id()
    if station_id is None:
        return jsonify({"ok": False, "error": "нет станций"}), 409
    return _start_station(station_id)


@app.route("/api/stop", methods=["POST"])
def api_stop():
    station_id = first_station_id()
    if station_id is None:
        return jsonify({"ok": False, "error": "нет станций"}), 409
    return _stop_station(station_id)


@app.route("/api/restart", methods=["POST"])
def api_restart():
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        try:
            save_config(body)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    station_id = first_station_id()
    if station_id is None:
        return jsonify({"ok": False, "error": "нет станций"}), 409
    return _restart_station(station_id)


@app.route("/api/status", methods=["GET"])
def api_status():
    station_id = first_station_id()
    if station_id is None:
        return jsonify({"ok": False, "error": "нет станций"}), 404
    cfg = _station_config_or_404(station_id)
    return jsonify(get_manager(station_id).status(cfg))


@app.route("/api/logs", methods=["GET"])
def api_logs():
    station_id = first_station_id()
    if station_id is None:
        return jsonify({"ok": False, "error": "нет станций"}), 404
    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        since = 0
    items, latest = get_manager(station_id).logs_since(since)
    return jsonify({"logs": items, "latest": latest})


# -------------------------------------------------------------- listening ---


def _ffmpeg_binary(cfg):
    binary = (cfg.get("ffmpeg_path") or "").strip() or "ffmpeg"
    if os.path.sep in binary or (os.path.altsep and os.path.altsep in binary):
        return binary
    return shutil.which(binary) or binary


def _listen_response(cfg, which, station_id=None):
    """Мониторинговый MP3-поток для прослушки в браузере (A/B вход/выход)."""
    if which == "input":
        url = cfg["source"]["url"]
        reconnect = cfg["source"].get("reconnect")
        delay_max = cfg["source"].get("reconnect_delay_max", 5)
    elif which == "output":
        ice = cfg["icecast"]
        url = "http://{}:{}/{}".format(
            ice["host"], ice["port"], str(ice["mount"]).lstrip("/"))
        reconnect, delay_max = False, 5
    else:
        return jsonify({"ok": False, "error": "unknown stream"}), 404

    cmd = [_ffmpeg_binary(cfg), "-hide_banner", "-loglevel", "error"]
    if reconnect:
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                "-reconnect_delay_max", str(delay_max)]
    cmd += ["-i", url, "-vn", "-c:a", "libmp3lame", "-b:a", "128k",
            "-f", "mp3", "pipe:1"]

    kwargs = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, **kwargs)
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    register_listener(station_id, proc)

    @stream_with_context
    def generate():
        try:
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                yield chunk
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                proc.kill()
            except OSError:
                pass
            unregister_listener(station_id, proc)

    return Response(generate(), mimetype="audio/mpeg", headers={
        "Cache-Control": "no-cache, no-store",
        "X-Accel-Buffering": "no",
    })


@app.route("/api/streams/<station_id>/listen/<which>")
def api_station_listen(station_id, which):
    cfg = _station_config_or_404(station_id)
    if cfg is None:
        return _station_not_found()
    return _listen_response(cfg, which, station_id)


@app.route("/api/listen/<which>")
def api_listen(which):
    station_id = first_station_id()
    if station_id is None:
        return jsonify({"ok": False, "error": "нет станций"}), 404
    cfg = _station_config_or_404(station_id)
    return _listen_response(cfg, which, station_id)


def main():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8770"))
    doc = load_streams()
    cfg = station_runtime_config(doc["streams"][0], doc["settings"]) if doc["streams"] else {"ffmpeg_path": ""}
    path, version = find_ffmpeg(cfg)
    print("=" * 60)
    print("  Мультипоточная обработка | панель управления")
    print("  Откройте в браузере: http://{}:{}".format(host, port))
    print("  Станций в реестре: {}".format(len(doc["streams"])))
    if path:
        print("  FFmpeg: {} ({})".format(path, version or "версия не определена"))
    else:
        print("  ВНИМАНИЕ: ffmpeg не найден. Установите FFmpeg или укажите")
        print("  путь к ffmpeg.exe в настройках панели.")
    print("=" * 60)
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
