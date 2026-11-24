"""Релей ICY-метаданных: StreamTitle источника -> Liquidsoap-воркер.

Зачем: Liquidsoap 2.4 (input.http на базе ffmpeg) не читает in-stream ICY-блоки
источника — воркер получает только заголовки icy-*. Поэтому актуальный
StreamTitle доставляет этот релей: читает ICY-блоки источника (заголовок
Icy-MetaData: 1) и при смене трека вставляет тег в воркер через telnet-команду
«panel.set_title <url-encoded>» (регистрируется в скрипте station.liq).
Дальше output.icecast рассылает тег слушателям штатным ICY-обновлением по
source-соединению — это работает с любой версией Icecast, в отличие от
admin API /admin/metadata (Icecast 2.5 кладёт updinfo в x_icy_title,
не в эфир).
"""

import socket
import ssl
import threading
import time
import urllib.parse


def _split_url(url):
    scheme, _, rest = url.partition("://")
    if not rest:
        scheme, rest = "http", scheme
    hostport, _, path = rest.partition("/")
    host, _, port = hostport.partition(":")
    port = int(port) if port else (443 if scheme == "https" else 80)
    return scheme, host, port, "/" + path


def _read_chunk(sock, n, buf):
    """Прочитать ровно n байт, используя и пополняя buf."""
    while len(buf) < n:
        data = sock.recv(min(65536, n - len(buf) + 4096))
        if not data:
            raise ConnectionError("источник закрыл соединение")
        buf += data
    return buf[:n], buf[n:]


class MetadataRelay:
    """Фоновый поток: StreamTitle источника -> telnet воркера."""

    # Минимальный интервал между попытками доставки одного тега.
    RETRY_INTERVAL = 10.0

    def __init__(self, station_id, source_url, telnet_provider, log_fn):
        self.station_id = station_id
        self.source_url = source_url
        # callable() -> (host, port) telnet-сервера воркера или None
        self._telnet_provider = telnet_provider
        self._log = log_fn
        self._stop = threading.Event()
        self._thread = None
        self._last_title = None     # последний увиденный в источнике
        self._pending_title = None  # увиденный, но ещё не доставленный
        self._last_push_attempt = 0.0

    # ------------------------------------------------------------ lifecycle

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._log("[panel] релей метаданных запущен ({})".format(self.source_url))

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
        self._thread = None

    # ------------------------------------------------------------- helpers

    def _push_title(self, title):
        """Отправить тег воркеру: telnet «panel.set_title <url-encoded>»."""
        hp = self._telnet_provider()
        if hp is None:
            raise ConnectionError("telnet воркера недоступен")
        host, port = hp
        encoded = urllib.parse.quote(title, safe="")
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.settimeout(5)
            sock.sendall("panel.set_title {}\n".format(encoded).encode("utf-8"))
            # Ответ liquidsoap завершается строкой «END»; читаем с таймаутом,
            # ошибки исполнения команды ловить не обязательно — достаточно
            # самого факта доставки команды.
            try:
                sock.recv(4096)
            except socket.timeout:
                pass

    # ---------------------------------------------------------------- loop

    def _run(self):
        while not self._stop.is_set():
            try:
                self._pump()
            except Exception as exc:
                if not self._stop.is_set():
                    self._log("[panel] релей метаданных: {} (повтор через 10 с)".format(exc))
            self._stop.wait(10)

    def _maybe_push(self):
        """Доставить ожидающий тег, если пришло время повторной попытки."""
        if self._pending_title is None:
            return
        now = time.time()
        if now - self._last_push_attempt < self.RETRY_INTERVAL:
            return
        self._last_push_attempt = now
        try:
            self._push_title(self._pending_title)
            self._log("[panel] метаданные -> воркер: {}".format(self._pending_title))
            self._pending_title = None
        except Exception as exc:
            self._log("[panel] релей метаданных: не удалось передать тег "
                      "воркеру: {}".format(exc))

    def _pump(self):
        scheme, host, port, path = _split_url(self.source_url)
        raw = socket.create_connection((host, port), timeout=10)
        try:
            if scheme == "https":
                ctx = ssl.create_default_context()
                sock = ctx.wrap_socket(raw, server_hostname=host)
            else:
                sock = raw
            sock.settimeout(15)
            req = ("GET {} HTTP/1.0\r\nHost: {}\r\nIcy-MetaData: 1\r\n"
                   "User-Agent: AudioPanel-MetadataRelay/1.0\r\n\r\n").format(path, host)
            sock.sendall(req.encode())

            data = b""
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("источник закрыл соединение до заголовков")
                data += chunk
            head, buf = data.split(b"\r\n\r\n", 1)
            headers = head.decode("latin-1", "replace")
            metaint = None
            for line in headers.split("\r\n"):
                if line.lower().startswith("icy-metaint:"):
                    metaint = int(line.split(":", 1)[1].strip())
            if not metaint:
                self._log("[panel] релей метаданных: источник без ICY-метаданных, "
                          "релей не нужен")
                # Источник без метаданных: дальше следить не за чем.
                return

            while not self._stop.is_set():
                block, buf = _read_chunk(sock, metaint, buf)  # аудио пропускаем
                del block
                size_raw, buf = _read_chunk(sock, 1, buf)
                mlen = size_raw[0] * 16
                if mlen:
                    payload, buf = _read_chunk(sock, mlen, buf)
                    title = self._parse_stream_title(payload)
                    if title is not None and title != self._last_title:
                        self._last_title = title
                        if title.strip(" -"):
                            self._pending_title = title
                # Блоки идут примерно раз в секунду (metaint при 128 кбит/с) —
                # удобный такт для повторной доставки при сбое.
                self._maybe_push()
        finally:
            try:
                raw.close()
            except Exception:
                pass

    @staticmethod
    def _parse_stream_title(payload):
        """Достать StreamTitle из ICY-блока или None, если его нет."""
        text = payload.rstrip(b"\x00").decode("utf-8", "replace")
        for part in text.split(";"):
            if part.lower().startswith("streamtitle="):
                value = part.split("=", 1)[1].strip()
                if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
                    value = value[1:-1]
                return value.strip()
        return None
