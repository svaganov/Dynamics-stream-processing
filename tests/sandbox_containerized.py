"""Песочница полной контейнеризации: панель В КОНТЕЙНЕРЕ + воркеры.

Проверяет главный сценарий плана контейнеризации: панель, запущенная в
контейнере с docker.sock, общим томом и общей сетью, сама поднимает
Liquidsoap-воркера (скрипт — с named volume, telnet — по имени контейнера,
порты воркера на хост не публикуются), поток доходит до Icecast на хосте,
живая подстройка работает.

Запуск из корня проекта:
    .test-venv/Scripts/python tests/sandbox_containerized.py

Требует: Docker Desktop (демон доступен). Образы панели (audio-panel:test)
и воркера (audio-worker:2.4.2) собираются при отсутствии.
"""

import json
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sandbox_integration as base  # noqa: E402  (источник + фейковый Icecast)

PANEL_IMAGE = "audio-panel:test"
WORKER_IMAGE = "audio-worker:2.4.2"
NETWORK = "ap-test-net"
VOLUME = "ap-test-vol"
PANEL_CONTAINER = "audio-panel-test"

DOCKER = r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
if not Path(DOCKER).exists():
    DOCKER = "docker"


def docker(*args, check=True, timeout=600):
    return subprocess.run([DOCKER] + list(args), check=check, timeout=timeout,
                          capture_output=True, text=True)


def api(port, method, path, payload=None):
    req = urllib.request.Request(
        "http://127.0.0.1:{}{}".format(port, path),
        method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


class ContainerizedPanelTest(unittest.TestCase):
    """Панель в контейнере оркестрирует воркера через sock/том/сеть."""

    @classmethod
    def setUpClass(cls):
        import docker
        client = docker.from_env()
        try:
            client.images.get(WORKER_IMAGE)
        except docker.errors.ImageNotFound:
            docker("build", "-t", WORKER_IMAGE, str(PROJECT_ROOT / "worker"))
        try:
            client.images.get(PANEL_IMAGE)
        except docker.errors.ImageNotFound:
            docker("build", "-f", str(PROJECT_ROOT / "app" / "Dockerfile"),
                   "-t", PANEL_IMAGE, str(PROJECT_ROOT))
        client.close()

    def setUp(self):
        if base.imageio_ffmpeg is None:
            self.skipTest("нужен пакет imageio-ffmpeg")
        self.ffmpeg = base.imageio_ffmpeg.get_ffmpeg_exe()

        raw_mp3 = Path(self._tmp("source-raw.mp3"))
        subprocess.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=120",
            "-vn", "-c:a", "libmp3lame", "-b:a", "64k", str(raw_mp3),
        ], check=True, timeout=60)
        body, actual_metaint = base.add_icy_metadata(raw_mp3.read_bytes(), "Containerized Title")

        self.source = base.SandboxServer(("127.0.0.1", 0), base.SourceHandler,
                                         source_body=body, raw_body=raw_mp3.read_bytes(),
                                         metaint=actual_metaint, throttle=32768)
        self.icecast = base.SandboxServer(("127.0.0.1", 0), base.FakeIcecastHandler,
                                          headers={}, bodies={}, metadata_updates={})
        threading.Thread(target=self.source.serve_forever, daemon=True).start()
        threading.Thread(target=self.icecast.serve_forever, daemon=True).start()

        # Чистый контур: свои сеть/том/контейнер панели.
        docker("rm", "-f", PANEL_CONTAINER, check=False)
        docker("network", "rm", NETWORK, check=False)
        docker("volume", "rm", VOLUME, check=False)
        docker("network", "create", NETWORK)
        docker("volume", "create", VOLUME)
        docker("run", "-d", "--name", PANEL_CONTAINER,
               "--network", NETWORK,
               "-p", "127.0.0.1::8770",
               "-v", "/var/run/docker.sock:/var/run/docker.sock",
               "-v", "{}:/data".format(VOLUME),
               "--add-host", "host.docker.internal:host-gateway",
               "-e", "WORKER_NETWORK={}".format(NETWORK),
               "-e", "WORKER_VOLUME={}".format(VOLUME),
               "-e", "WORKER_IMAGE={}".format(WORKER_IMAGE),
               PANEL_IMAGE)
        self.panel_port = self._panel_port()
        self._wait_panel()

    def tearDown(self):
        docker("rm", "-f", PANEL_CONTAINER, check=False)
        # воркеры, которых могла оставить панель
        out = docker("ps", "-a", "--filter", "label=audio-panel=1",
                     "--format", "{{.Names}}", check=False)
        for name in out.stdout.split():
            docker("rm", "-f", name, check=False)
        docker("network", "rm", NETWORK, check=False)
        docker("volume", "rm", VOLUME, check=False)
        self.source.shutdown()
        self.icecast.shutdown()
        self.source.server_close()
        self.icecast.server_close()
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None

    _tmpdir = None

    def _tmp(self, name):
        import tempfile
        if self._tmpdir is None:
            self._tmpdir = tempfile.TemporaryDirectory()
        return str(Path(self._tmpdir.name) / name)

    def _panel_port(self):
        deadline = time.time() + 15
        while time.time() < deadline:
            out = docker("port", PANEL_CONTAINER, "8770", check=False).stdout.strip()
            if out:
                return int(out.rsplit(":", 1)[1])
            time.sleep(0.5)
        self.fail("порт панели не опубликован")

    def _wait_panel(self, timeout=90):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                api(self.panel_port, "GET", "/api/streams")
                return
            except Exception:
                time.sleep(1)
        logs = docker("logs", "--tail", "30", PANEL_CONTAINER, check=False)
        self.fail("панель в контейнере не поднялась; логи:\n{}\n{}".format(
            logs.stdout, logs.stderr))

    def test_panel_in_container_runs_worker(self):
        # Станция: источник и Icecast — на хосте (127.0.0.1): и воркер, и
        # подслушка внутри контейнеров должны достать их через
        # host.docker.internal (перезапись loopback).
        created = api(self.panel_port, "POST", "/api/streams",
                      {"id": "ct-1", "name": "Контейнерная"})["station"]
        sid = created["id"]
        res = api(self.panel_port, "PUT", "/api/streams/" + sid, {
            "source": {"url": "http://127.0.0.1:{}/source.mp3".format(
                self.source.server_address[1])},
            "icecast": {"host": "127.0.0.1", "port": self.icecast.server_address[1],
                        "mount": "/ct-1"},
            "autorestart": {"enabled": False, "delay": 1},
        })
        self.assertTrue(res["ok"], res)

        res = api(self.panel_port, "POST", "/api/streams/{}/start".format(sid))
        self.assertTrue(res["ok"], res)

        # Поток дошёл до Icecast на хосте — валидный ADTS.
        deadline = time.time() + 60
        body = b""
        while time.time() < deadline:
            body = bytes(self.icecast.bodies.get("/ct-1", b""))
            if len(body) >= 20000:
                break
            time.sleep(0.3)
        self.assertGreaterEqual(len(body), 20000,
                                "Icecast не получил поток от воркера")
        self.assertTrue(body.startswith(b"\xff\xf1") or body.startswith(b"\xff\xf9"),
                        "не ADTS")

        # Воркер создан в общей сети, без публикации портов, с томом read-only.
        import docker
        client = docker.from_env()
        worker = client.containers.get("audio-worker-ct-1")
        self.assertIn(NETWORK, worker.attrs["NetworkSettings"]["Networks"])
        ports = worker.attrs["NetworkSettings"]["Ports"]
        self.assertFalse([b for binds in ports.values() if binds for b in binds],
                         "порты воркера не должны публиковаться на хост")
        mounts = worker.attrs["Mounts"]
        self.assertTrue(any(m.get("Name") == VOLUME and not m.get("RW")
                            for m in mounts), mounts)

        # Живая подстройка через telnet по имени контейнера (без портов хоста).
        res = api(self.panel_port, "POST", "/api/streams/{}/adjust".format(sid),
                  {"gain_db": 6})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["mode"], "live")

        # Стоп — воркер остановлен, статусы честные.
        res = api(self.panel_port, "POST", "/api/streams/{}/stop".format(sid))
        self.assertTrue(res["ok"], res)
        client.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
