"""Медиапесочница Этапа 2: FFmpeg-станция + Liquidsoap-воркер в Docker.

Поднимает локальный HTTP-источник MP3 с ICY StreamTitle и фейковый Icecast,
запускает station-1 через процесс FFmpeg (хост) и station-2 через контейнер
Liquidsoap (доступ к хосту — через host.docker.internal), проверяет:

- обе станции реально кодируют AAC/ADTS и пушат на Icecast;
- StreamTitle доезжает: у FFmpeg — через MetadataRelay (/admin/metadata),
  у Liquidsoap — сквозь конвейер (виден в его логе);
- остановка FFmpeg-станции не трогает Docker-воркера и наоборот.

Запуск из корня проекта:
    .test-venv/Scripts/python tests/sandbox_liquidsoap.py

Требует: Docker Desktop (демон доступен), образ audio-worker:2.4.2
(собирается: docker build -t audio-worker:2.4.2 worker).
"""

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sandbox_integration as base  # noqa: E402  (поднимает panel, серверы-помощники)

panel = base.panel

HOST_DOCKER_INTERNAL = "host.docker.internal"


def host_url(port, path):
    return "http://{}:{}{}".format(HOST_DOCKER_INTERNAL, port, path)


class LiquidsoapSandboxTest(unittest.TestCase):
    title = base.IntegrationSandboxTest.title

    @classmethod
    def setUpClass(cls):
        # Образ воркера должен существовать; при необходимости собираем.
        import docker
        client = docker.from_env()
        try:
            client.images.get(panel.WORKER_IMAGE)
        except docker.errors.ImageNotFound:
            subprocess.run(
                ["docker", "build", "-t", panel.WORKER_IMAGE, str(PROJECT_ROOT / "worker")],
                check=True, timeout=600,
            )
        client.close()

    def setUp(self):
        if base.imageio_ffmpeg is None:
            self.skipTest("нужен пакет imageio-ffmpeg")
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.ffmpeg = base.imageio_ffmpeg.get_ffmpeg_exe()

        raw_mp3 = self.tmp_path / "source-raw.mp3"
        subprocess.run([
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
            "-vn", "-c:a", "libmp3lame", "-b:a", "64k", str(raw_mp3),
        ], check=True, timeout=30)
        source_body, actual_metaint = base.add_icy_metadata(raw_mp3.read_bytes(), self.title)
        self.assertIn(self.title.encode(), source_body)

        self.source = base.SandboxServer(("127.0.0.1", 0), base.SourceHandler,
                                         source_body=source_body, raw_body=raw_mp3.read_bytes(),
                                         metaint=actual_metaint, throttle=32768)
        self.icecast = base.SandboxServer(("127.0.0.1", 0), base.FakeIcecastHandler,
                                          headers={}, bodies={}, metadata_updates={})
        threading.Thread(target=self.source.serve_forever, daemon=True).start()
        threading.Thread(target=self.icecast.serve_forever, daemon=True).start()

        panel.STREAMS_PATH = str(self.tmp_path / "streams.json")
        panel.LEGACY_CONFIG_PATH = str(self.tmp_path / "config.json")
        panel.RUNTIME_DIR = str(self.tmp_path / "runtime")
        panel._managers.clear()
        panel._listeners.clear()
        panel.app.config["TESTING"] = True
        self.client = panel.app.test_client()

        self.client.put("/api/settings", json={"ffmpeg_path": self.ffmpeg})
        # station-1 — FFmpeg-процесс на хосте, источник 127.0.0.1
        self.client.put("/api/streams/station-1", json={
            "source": {"url": self.source_url("/source.mp3"), "reconnect": False, "reconnect_delay_max": 1},
            "icecast": {"host": "127.0.0.1", "port": self.icecast_port, "mount": "/station-1"},
            "autorestart": {"enabled": False, "delay": 1},
        })
        # station-2 — Liquidsoap-воркер в контейнере, хост — через host.docker.internal
        created = self.client.post("/api/streams", json={"name": "Радио Liq"}).get_json()["station"]
        self.station2_id = created["id"]
        self.client.put("/api/streams/" + self.station2_id, json={
            "engine": "liquidsoap",
            "source": {"url": host_url(self.source_port, "/source.mp3"), "reconnect": True, "reconnect_delay_max": 1},
            "icecast": {"host": HOST_DOCKER_INTERNAL, "port": self.icecast_port, "mount": "/station-2"},
            "autorestart": {"enabled": False, "delay": 1},
            "equalizer": {"enabled": True, "gains": [2, 0, 0, 0, 1, 0, 0, 0, 0, 0]},
        })

    def tearDown(self):
        for manager in list(panel._managers.values()):
            if hasattr(manager, "dispose"):
                manager.dispose()
            else:
                manager.stop()
        panel._managers.clear()
        for station_id in list(panel._listeners):
            panel.stop_listeners(station_id)
        self.source.shutdown()
        self.icecast.shutdown()
        self.source.server_close()
        self.icecast.server_close()
        self.tmp.cleanup()

    @property
    def icecast_port(self):
        return self.icecast.server_address[1]

    @property
    def source_port(self):
        return self.source.server_address[1]

    def source_url(self, path):
        return "http://127.0.0.1:{}{}".format(self.source_port, path)

    def wait_logs_contain(self, station_id, needle, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            items = self.client.get("/api/streams/{}/logs?since=0".format(station_id)).get_json()["logs"]
            if any(needle in item["line"] for item in items):
                return [i["line"] for i in items]
            time.sleep(0.25)
        items = self.client.get("/api/streams/{}/logs?since=0".format(station_id)).get_json()["logs"]
        self.fail("в логах {} не найдено {!r}; последние строки:\n{}".format(
            station_id, needle, "\n".join(i["line"] for i in items[-30:])))

    def wait_for_body(self, mount, minimum, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            body = bytes(self.icecast.bodies.get(mount, b""))
            if len(body) >= minimum:
                return body
            time.sleep(0.2)
        self.fail("Icecast не получил {} байт для {}".format(minimum, mount))

    def test_mixed_engines_end_to_end(self):
        self.assertTrue(self.client.post("/api/streams/station-1/start").get_json()["ok"])
        res = self.client.post("/api/streams/" + self.station2_id + "/start").get_json()
        self.assertTrue(res["ok"], res)

        body1 = self.wait_for_body("/station-1", 20000)
        body2 = self.wait_for_body("/station-2", 20000)
        self.assertTrue(body1.startswith(b"\xff\xf1") or body1.startswith(b"\xff\xf9"), "station-1 не ADTS")
        self.assertTrue(body2.startswith(b"\xff\xf1") or body2.startswith(b"\xff\xf9"), "station-2 (Liquidsoap) не ADTS")

        # StreamTitle: FFmpeg-станция — через MetadataRelay на /admin/metadata;
        # Liquidsoap проворачивает метаданные сквозь конвейер — видно в его логе.
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.icecast.metadata_updates.get("/station-1") == self.title:
                break
            time.sleep(0.2)
        self.assertEqual(self.icecast.metadata_updates.get("/station-1"), self.title)
        self.wait_logs_contain(self.station2_id, self.title)

        # Статусы и изоляция: остановка FFmpeg-станции не трогает воркера.
        st2 = self.client.get("/api/streams/" + self.station2_id + "/status").get_json()
        self.assertEqual(st2["engine"], "liquidsoap")
        self.assertTrue(st2["running"])
        self.assertTrue(st2["container"].startswith("audio-worker-"))

        self.assertTrue(self.client.post("/api/streams/station-1/stop").get_json()["ok"])
        time.sleep(1.5)
        st2_after = self.client.get("/api/streams/" + self.station2_id + "/status").get_json()
        self.assertTrue(st2_after["running"], "остановка FFmpeg-станции остановила воркер")
        self.assertEqual(st2["pid"], st2_after["pid"])

        # И обратная изоляция: остановка воркера не трогает FFmpeg-станцию.
        self.assertTrue(self.client.post("/api/streams/station-1/start").get_json()["ok"])
        self.assertTrue(self.client.post("/api/streams/" + self.station2_id + "/stop").get_json()["ok"])
        time.sleep(1.5)
        st1_after = self.client.get("/api/streams/station-1/status").get_json()
        self.assertTrue(st1_after["running"], "остановка воркера остановила FFmpeg-станцию")
        st2_stopped = self.client.get("/api/streams/" + self.station2_id + "/status").get_json()
        self.assertFalse(st2_stopped["running"])

        health = self.client.get("/api/health").get_json()
        engines = {s["id"]: s["engine"] for s in health["stations"]}
        self.assertEqual(engines[self.station2_id], "liquidsoap")


if __name__ == "__main__":
    unittest.main(verbosity=2)
