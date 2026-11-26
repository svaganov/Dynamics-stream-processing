import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = PROJECT_ROOT / "app" / "app.py"

spec = importlib.util.spec_from_file_location("panel_app", APP_PATH)
panel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(panel)


class FakeStderr:
    def __init__(self, proc):
        self.proc = proc

    def __iter__(self):
        return self

    def __next__(self):
        if self.proc.finished:
            raise StopIteration
        time.sleep(0.02)
        return "fake ffmpeg stderr\n"


class FakeProc:
    def __init__(self, pid, popen_calls):
        self.pid = pid
        self.popen_calls = popen_calls
        self.alive = True
        self.finished = False
        self.terminated = False
        self.stderr = FakeStderr(self)
        # Подслушка читает proc.stdout: пустой поток = конец мониторинга.
        import io
        self.stdout = io.BytesIO(b"")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.alive:
            self.kill()
        return False

    def communicate(self, input=None, timeout=None):
        self.wait(timeout=timeout)
        return "", ""

    def poll(self):
        return None if self.alive else 0

    def wait(self, timeout=None):
        deadline = time.time() + (timeout or 5)
        while self.alive and time.time() < deadline:
            time.sleep(0.01)
        self.finished = True
        return 0

    def terminate(self):
        self.terminated = True
        self.alive = False

    def kill(self):
        self.terminated = True
        self.alive = False

    def die(self):
        self.alive = False
        self.finished = True


class FakeLiquidsoapContainer:
    """Эмуляция запущенного Docker-контейнера воркера Liquidsoap."""

    def __init__(self, host_ip="127.0.0.1", host_port="55999"):
        self.status = "running"
        self.attrs = {
            "NetworkSettings": {
                "Ports": {
                    "1234/tcp": [{"HostIp": host_ip, "HostPort": host_port}],
                },
            },
        }

    def reload(self):
        pass


class FakeWorkerContainer:
    """Полная эмуляция контейнера воркера для юнит-тестов без демона Docker."""

    def __init__(self, name, pid):
        self.name = name
        self.pid = pid
        self.status = "created"
        self.stopped = False
        self.removed = False
        self.create_kwargs = {}
        self.attrs = {
            "NetworkSettings": {"Ports": {}},
            "State": {"Pid": None, "StartedAt": "", "ExitCode": None},
        }

    def start(self):
        from datetime import datetime, timezone
        self.status = "running"
        self.attrs["State"]["Pid"] = self.pid
        self.attrs["State"]["StartedAt"] = datetime.now(timezone.utc).isoformat()

    def stop(self, timeout=5):
        self.stopped = True
        self.status = "exited"
        self.attrs["State"]["Pid"] = None

    def kill(self):
        self.stopped = True
        self.status = "exited"
        self.attrs["State"]["Pid"] = None

    def remove(self, force=False):
        self.removed = True

    def reload(self):
        pass

    def logs(self, **kwargs):
        return iter(())

    def die(self):
        # аварийное завершение воркера (контейнер умер сам)
        self.status = "exited"
        self.attrs["State"]["Pid"] = None
        self.attrs["State"]["ExitCode"] = 1


class FakeDockerContainers:
    def __init__(self):
        self.created = []

    def list(self, all=True, filters=None):
        name = (filters or {}).get("name")
        return [c for c in self.created if c.name == name and not c.removed]

    def create(self, image, name=None, **kwargs):
        container = FakeWorkerContainer(name, 1000 + len(self.created))
        container.create_kwargs = kwargs
        self.created.append(container)
        return container


class FakeDockerNetworks:
    def __init__(self):
        self.created = []

    def list(self, names=None):
        return [n for n in self.created if not names or n in names]

    def create(self, name, driver="bridge"):
        self.created.append(name)
        return name


class FakeDockerClient:
    def __init__(self):
        self.containers = FakeDockerContainers()
        self.networks = FakeDockerNetworks()


class PanelApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        panel.STREAMS_PATH = str(self.tmp_path / "streams.json")
        panel.LEGACY_CONFIG_PATH = str(self.tmp_path / "config.json")
        panel._managers.clear()
        panel.app.config["TESTING"] = True
        self.client = panel.app.test_client()
        self.fake_ffmpeg = self.tmp_path / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        self.fake_ffmpeg.write_text("fake", encoding="ascii")

    def tearDown(self):
        for manager in list(panel._managers.values()):
            manager.stop()
        panel._managers.clear()
        self.tmp.cleanup()

    def make_fake_docker(self):
        """Подменяет демон Docker фейковым клиентом (воркеры-контейнеры)."""
        client = FakeDockerClient()
        patcher = mock.patch.object(
            panel.DockerWorkerManager, "_docker_client", return_value=client)
        return patcher, client

    def make_fake_popen(self):
        calls = []
        procs = []

        def fake_popen(cmd, **kwargs):
            proc = FakeProc(1000 + len(calls), calls)
            proc.args = cmd
            calls.append(cmd)
            procs.append(proc)
            return proc

        return mock.patch.object(panel.subprocess, "Popen", side_effect=fake_popen), calls, procs

    def test_registry_crud_and_validation(self):
        res = self.client.get("/api/streams")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(len(data["streams"]), 1)
        self.assertEqual(data["streams"][0]["id"], "station-1")

        res = self.client.post("/api/streams", json={"name": "Радио 2"})
        self.assertEqual(res.status_code, 201)
        station2 = res.get_json()["station"]
        self.assertEqual(station2["id"], "station-2")
        # у новой станции один выход по умолчанию, mount — по id станции
        self.assertEqual(len(station2["outputs"]), 1)
        self.assertEqual(station2["outputs"][0]["icecast"]["mount"], "/station-2")

        res = self.client.post("/api/streams", json={"id": "station-2", "name": "Дубликат"})
        self.assertEqual(res.status_code, 409)

        # точка выхода (host, port, mount) уникальна глобально
        res = self.client.put(
            "/api/streams/station-2",
            json={"outputs": [{"icecast": {"mount": "/station-1"}}]},
        )
        self.assertEqual(res.status_code, 409)

        res = self.client.put(
            "/api/streams/station-2",
            json={"source": {"url": "http://127.0.0.1:9000/radio2"}},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["station"]["source"]["url"], "http://127.0.0.1:9000/radio2")

        health = self.client.get("/api/health").get_json()
        self.assertEqual(health["counts"]["total"], 2)
        self.assertEqual(health["counts"]["running"], 0)
        self.assertEqual(health["counts"]["stopped"], 2)

        self.assertEqual(self.client.delete("/api/streams/missing").status_code, 404)

    def test_registry_export_import(self):
        # Две станции с разными точками выхода
        self.client.post("/api/streams", json={"name": "Радио 2"})
        self.client.put("/api/streams/station-2",
                        json={"source": {"url": "http://127.0.0.1:9000/radio2"}})

        # Экспорт: JSON-файл на скачивание с полным реестром
        res = self.client.get("/api/registry/export")
        self.assertEqual(res.status_code, 200)
        self.assertIn("attachment", res.headers.get("Content-Disposition", ""))
        exported = json.loads(res.data.decode("utf-8"))
        self.assertEqual([s["id"] for s in exported["streams"]],
                         ["station-1", "station-2"])
        self.assertEqual(exported["streams"][1]["source"]["url"],
                         "http://127.0.0.1:9000/radio2")

        # Импорт заменяет реестр и делает бэкап прежнего streams.json
        exported["streams"] = [s for s in exported["streams"]
                               if s["id"] == "station-2"]
        res = self.client.post("/api/registry/import", json=exported)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {"ok": True, "stations": 1})
        data = self.client.get("/api/streams").get_json()
        self.assertEqual([s["id"] for s in data["streams"]], ["station-2"])
        backups = list(self.tmp_path.glob("streams.json.bak-*"))
        self.assertEqual(len(backups), 1)

        # Некорректный файл не трогает реестр
        res = self.client.post("/api/registry/import", json={"foo": []})
        self.assertEqual(res.status_code, 400)
        self.assertIn("streams", res.get_json()["error"])
        dup = {"streams": [{"id": "a"}, {"id": "a"}]}
        res = self.client.post("/api/registry/import", json=dup)
        self.assertEqual(res.status_code, 400)
        self.assertIn("дважды", res.get_json()["error"])
        data = self.client.get("/api/streams").get_json()
        self.assertEqual([s["id"] for s in data["streams"]], ["station-2"])

    def test_non_json_body_rejected_not_silently_defaulted(self):
        # Тело без Content-Type: application/json раньше молча превращалось
        # в дефолтную станцию — конфиг клиента терялся. Теперь — явная 400.
        res = self.client.post("/api/streams", data='{"id": "cloud-1"}',
                               content_type="text/plain")
        self.assertEqual(res.status_code, 400)
        data = self.client.get("/api/streams").get_json()
        self.assertEqual([s["id"] for s in data["streams"]], ["station-1"])

        res = self.client.put("/api/streams/station-1", data='{"name": "X"}',
                              content_type="text/plain")
        self.assertEqual(res.status_code, 400)

        # Пустое тело по-прежнему допустимо: UI «+ Поток» создаёт дефолтную станцию.
        res = self.client.post("/api/streams")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.get_json()["station"]["id"], "station-2")

    def test_legacy_config_migrates_to_station_one(self):
        legacy = {
            "source": {"url": "http://127.0.0.1:8000/input", "reconnect": True, "reconnect_delay_max": 7},
            "icecast": {"host": "127.0.0.1", "port": 8000, "mount": "/stream", "user": "source", "password": "secret"},
            "ffmpeg_path": "C:/ffmpeg/bin/ffmpeg.exe",
        }
        Path(panel.LEGACY_CONFIG_PATH).write_text(json.dumps(legacy), encoding="utf-8")

        data = self.client.get("/api/streams").get_json()
        station = data["streams"][0]
        self.assertEqual(station["id"], "station-1")
        self.assertEqual(station["source"]["reconnect_delay_max"], 7)
        # одиночные encoder/icecast старого конфига мигрировали в outputs[0]
        self.assertNotIn("icecast", station)
        self.assertNotIn("encoder", station)
        self.assertEqual(station["outputs"][0]["icecast"]["mount"], "/stream")
        self.assertEqual(station["outputs"][0]["icecast"]["password"], "secret")
        self.assertEqual(data["settings"]["ffmpeg_path"], "C:/ffmpeg/bin/ffmpeg.exe")

        flat = self.client.get("/api/config").get_json()
        self.assertEqual(flat["source"]["url"], "http://127.0.0.1:8000/input")
        self.assertEqual(flat["ffmpeg_path"], "C:/ffmpeg/bin/ffmpeg.exe")

    def test_command_masks_icecast_password(self):
        res = self.client.post("/api/streams/station-1/command", json={
            "outputs": [{"id": "out-1", "icecast": {"password": "super-secret"}}],
        })
        self.assertEqual(res.status_code, 200)
        text = res.get_json()["text"]
        # предпросмотр — это скрипт воркера, пароль в нём замаскирован
        self.assertIn('password="***"', text)
        self.assertNotIn("super-secret", text)

    def test_gain_in_liq_chain(self):
        res = self.client.post("/api/streams/station-1/command", json={
            "equalizer": {"enabled": True, "gains": [3] + [0] * 9},
            "gain": {"enabled": True, "db": 2.5},
            "compressor": {"enabled": True},
        })
        text = res.get_json()["text"]
        pos_eq = text.index("filter.iir.eq.peak(frequency=31")
        pos_gain = text.index("gain_stage = amplify(panel_gain")
        pos_comp = text.index("comp_stage = compress(")
        self.assertLess(pos_eq, pos_gain)
        self.assertLess(pos_gain, pos_comp)

        # выключенное усиление не попадает в цепочку
        res = self.client.post("/api/streams/station-1/command", json={
            "gain": {"enabled": False, "db": 0},
        })
        self.assertNotIn("gain_stage", res.get_json()["text"])

        # кламп диапазона ±24 дБ через API станции
        updated = self.client.put("/api/streams/station-1", json={
            "gain": {"enabled": True, "db": 40},
        }).get_json()["station"]
        self.assertEqual(updated["gain"]["db"], 24.0)

    def test_adjust_requires_gain_db(self):
        res = self.client.post("/api/streams/station-1/adjust", json={})
        self.assertEqual(res.status_code, 400)
        res = self.client.post("/api/streams/station-1/adjust", json={"gain_db": "abc"})
        self.assertEqual(res.status_code, 400)
        res = self.client.post("/api/streams/station-1/adjust", json={"gain_db": None})
        self.assertEqual(res.status_code, 400)

    def test_adjust_rejected_when_worker_not_running(self):
        res = self.client.post("/api/streams", json={"name": "Радио LQ", "engine": "liquidsoap"})
        station_id = res.get_json()["station"]["id"]
        self.assertEqual(res.get_json()["station"]["engine"], "liquidsoap")

        res = self.client.post("/api/streams/{}/adjust".format(station_id), json={"gain_db": 3.0})
        self.assertEqual(res.status_code, 409)
        self.assertIn("не запущен", res.get_json()["error"])

    def test_adjust_sends_telnet_command_and_persists_gain(self):
        res = self.client.post("/api/streams", json={"name": "Радио LQ", "engine": "liquidsoap"})
        station_id = res.get_json()["station"]["id"]
        manager = panel.get_manager(station_id)
        manager.container = FakeLiquidsoapContainer()

        with mock.patch.object(panel, "_telnet_command", return_value="OK\nEND") as tel:
            res = self.client.post(
                "/api/streams/{}/adjust".format(station_id), json={"gain_db": 3.0})

        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["mode"], "live")
        self.assertEqual(data["gain"], {"enabled": True, "db": 3.0})
        # 3 дБ -> линейное усиление 10 ** (3/20) = 1.412538
        tel.assert_called_once_with("127.0.0.1", 55999, "var.set panel_gain = 1.412538")

        # Значение усиления сохранилось в реестре станции.
        station = self.client.get("/api/streams/" + station_id).get_json()["station"]
        self.assertTrue(station["gain"]["enabled"])
        self.assertEqual(station["gain"]["db"], 3.0)

    def test_adjust_reports_telnet_error(self):
        res = self.client.post("/api/streams", json={"name": "Радио LQ", "engine": "liquidsoap"})
        station_id = res.get_json()["station"]["id"]
        manager = panel.get_manager(station_id)
        manager.container = FakeLiquidsoapContainer()

        # Liquidsoap сообщает об ошибке строкой «Syntax error or variable not found.»
        with mock.patch.object(panel, "_telnet_command",
                               return_value="Syntax error or variable not found.\nEND"):
            res = self.client.post(
                "/api/streams/{}/adjust".format(station_id), json={"gain_db": -2.0})
        self.assertEqual(res.status_code, 409)
        self.assertIn("error", res.get_json()["error"].lower())

    def _make_running_liquidsoap_station(self):
        res = self.client.post("/api/streams", json={"name": "Радио LQ", "engine": "liquidsoap"})
        station_id = res.get_json()["station"]["id"]
        manager = panel.get_manager(station_id)
        manager.container = FakeLiquidsoapContainer()
        return station_id, manager

    def test_bypass_rejected_when_worker_not_running(self):
        res = self.client.post("/api/streams", json={"name": "Радио LQ", "engine": "liquidsoap"})
        station_id = res.get_json()["station"]["id"]
        res = self.client.post("/api/streams/{}/bypass".format(station_id), json={"enabled": True})
        self.assertEqual(res.status_code, 409)
        self.assertIn("не запущен", res.get_json()["error"])

    def test_bypass_sends_telnet_command_and_persists(self):
        station_id, _ = self._make_running_liquidsoap_station()
        with mock.patch.object(panel, "_telnet_command",
                               return_value="Variable panel_bypass set.\nEND") as tel:
            res = self.client.post(
                "/api/streams/{}/bypass".format(station_id), json={"enabled": True})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["mode"], "live")
        self.assertTrue(data["bypass"])
        tel.assert_called_once_with("127.0.0.1", 55999, "var.set panel_bypass = true")
        station = self.client.get("/api/streams/" + station_id).get_json()["station"]
        self.assertTrue(station["bypass"])

        # выключение шлёт false и тоже сохраняется
        with mock.patch.object(panel, "_telnet_command",
                               return_value="Variable panel_bypass set.\nEND") as tel:
            res = self.client.post(
                "/api/streams/{}/bypass".format(station_id), json={"enabled": False})
        self.assertEqual(res.status_code, 200)
        tel.assert_called_once_with("127.0.0.1", 55999, "var.set panel_bypass = false")
        station = self.client.get("/api/streams/" + station_id).get_json()["station"]
        self.assertFalse(station["bypass"])

    def test_bypass_reports_telnet_error(self):
        station_id, _ = self._make_running_liquidsoap_station()
        with mock.patch.object(panel, "_telnet_command",
                               return_value="Syntax error or variable not found.\nEND"):
            res = self.client.post(
                "/api/streams/{}/bypass".format(station_id), json={"enabled": True})
        self.assertEqual(res.status_code, 409)
        # значение не сохранилось
        station = self.client.get("/api/streams/" + station_id).get_json()["station"]
        self.assertFalse(station["bypass"])

    def test_outputs_connected_reads_per_output_flags(self):
        # У каждого включённого выхода — свой флаг panel_ice_connected_<id>:
        # true/false — состояние; «variable not found» (старый скрипт) и обрыв
        # telnet — None (неизвестно). Выключенные выходы не опрашиваются.
        # Опрос идёт по постоянной telnet-сессии (_ice_telnet), чтобы не
        # спамить лог воркера парами «New client / disconnected».
        station_id, manager = self._make_running_liquidsoap_station()
        cfg = {"outputs": [{"id": "out-1", "enabled": True},
                           {"id": "mobile", "enabled": True},
                           {"id": "off", "enabled": False}]}
        replies = {"var.get panel_ice_connected_out_1": "true\nEND",
                   "var.get panel_ice_connected_mobile": "false\nEND"}

        with mock.patch.object(manager, "_ice_telnet",
                               side_effect=lambda cmd: replies[cmd]) as tel:
            per = manager.outputs_connected(cfg)
        self.assertEqual(per, {"out-1": True, "mobile": False})
        self.assertEqual(tel.call_count, 2)  # выключенный выход не опрашивается

        # агрегат: все включённые подключены — True; хотя бы один нет — False
        with mock.patch.object(manager, "_ice_telnet",
                               side_effect=lambda cmd: replies[cmd]):
            self.assertIs(manager.icecast_connected(cfg), False)
        replies["var.get panel_ice_connected_mobile"] = "true\nEND"
        with mock.patch.object(manager, "_ice_telnet",
                               side_effect=lambda cmd: replies[cmd]):
            self.assertIs(manager.icecast_connected(cfg), True)

        # флаг не прочитался («variable not found» — старый скрипт): неизвестно
        replies["var.get panel_ice_connected_mobile"] = \
            "Syntax error or variable not found.\nEND"
        with mock.patch.object(manager, "_ice_telnet",
                               side_effect=lambda cmd: replies[cmd]):
            self.assertIsNone(manager.icecast_connected(cfg))
            self.assertEqual(manager.outputs_connected(cfg),
                             {"out-1": True, "mobile": None})

        # обрыв telnet — None целиком
        with mock.patch.object(manager, "_ice_telnet", side_effect=OSError("closed")):
            self.assertIsNone(manager.outputs_connected(cfg))

        # воркер не запущен — None, telnet не дёргается
        manager.container = None
        with mock.patch.object(manager, "_ice_telnet") as tel:
            self.assertIsNone(manager.outputs_connected(cfg))
            tel.assert_not_called()

        # без конфига (воркер не стартовал) — None, telnet не дёргается
        manager.container = FakeLiquidsoapContainer()
        with mock.patch.object(manager, "_ice_telnet") as tel:
            self.assertIsNone(manager.icecast_connected())
            tel.assert_not_called()

    def test_input_connected_reads_input_status(self):
        # «connected <url>» — вход на связи; прочее состояние — входа нет;
        # unknown command/обрыв telnet — None (неизвестно)
        station_id, manager = self._make_running_liquidsoap_station()

        with mock.patch.object(manager, "_ice_telnet",
                               return_value="connected http://host:8000/live\nEND") as tel:
            self.assertIs(manager.input_connected(), True)
        tel.assert_called_once_with("input.status")

        with mock.patch.object(manager, "_ice_telnet",
                               return_value="connecting http://host:8000/live\nEND"):
            self.assertIs(manager.input_connected(), False)

        with mock.patch.object(manager, "_ice_telnet",
                               return_value="ERROR: unknown command\nEND"):
            self.assertIsNone(manager.input_connected())

        with mock.patch.object(manager, "_ice_telnet", side_effect=OSError("closed")):
            self.assertIsNone(manager.input_connected())

        manager.container = None
        with mock.patch.object(manager, "_ice_telnet") as tel:
            self.assertIsNone(manager.input_connected())
            tel.assert_not_called()

    def test_adjust_requires_any_parameter(self):
        res = self.client.post("/api/streams/station-1/adjust", json={})
        self.assertEqual(res.status_code, 400)
        self.assertIn("gain_db и/или eq_gains", res.get_json()["error"])

    def test_adjust_eq_validation(self):
        station_id, _ = self._make_running_liquidsoap_station()
        res = self.client.post(
            "/api/streams/{}/adjust".format(station_id), json={"eq_gains": [1, 2, 3]})
        self.assertEqual(res.status_code, 400)
        res = self.client.post(
            "/api/streams/{}/adjust".format(station_id),
            json={"eq_gains": ["a"] * 10})
        self.assertEqual(res.status_code, 400)

    def test_adjust_eq_rejected_for_ffmpeg_engine(self):
        res = self.client.post(
            "/api/streams/station-1/adjust",
            json={"eq_gains": [0.0] * 10})
        self.assertEqual(res.status_code, 409)

    def test_adjust_eq_sends_telnet_session_and_persists(self):
        station_id, _ = self._make_running_liquidsoap_station()
        gains = [0.0] * 10
        gains[2] = 6.0   # 125 Гц
        gains[9] = -1.5  # 16 кГц
        with mock.patch.object(panel, "_telnet_session",
                               return_value=["OK\nEND"] * 10) as sess:
            res = self.client.post(
                "/api/streams/{}/adjust".format(station_id), json={"eq_gains": gains})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["mode"], "live")
        self.assertEqual(len(sess.call_args[0][2]), 10)
        self.assertEqual(sess.call_args[0][2][2], "var.set panel_eq_125 = 6.0")
        self.assertEqual(sess.call_args[0][2][9], "var.set panel_eq_16000 = -1.5")
        station = self.client.get("/api/streams/" + station_id).get_json()["station"]
        self.assertTrue(station["equalizer"]["enabled"])
        self.assertEqual(station["equalizer"]["gains"], gains)

    def test_adjust_compressor_sends_telnet_vars_and_persists(self):
        station_id, _ = self._make_running_liquidsoap_station()
        with mock.patch.object(panel, "_telnet_session",
                               return_value=["OK\nEND"] * 5) as sess:
            res = self.client.post(
                "/api/streams/{}/adjust".format(station_id),
                json={"compressor": {"threshold": -24, "ratio": 6,
                                     "attack": 3, "release": 200, "makeup": 4}})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        commands = sess.call_args[0][2]
        self.assertEqual(len(commands), 5)
        self.assertIn("var.set panel_comp_threshold = -24.0", commands)
        self.assertIn("var.set panel_comp_ratio = 6.0", commands)
        self.assertIn("var.set panel_comp_makeup = 4.0", commands)
        station = self.client.get("/api/streams/" + station_id).get_json()["station"]
        self.assertTrue(station["compressor"]["enabled"])
        self.assertEqual(station["compressor"]["threshold"], -24.0)
        self.assertEqual(station["compressor"]["ratio"], 6.0)

    def test_adjust_compressor_block_disabled_at_start_is_friendly(self):
        # Переменных panel_comp_* нет в скрипте воркера, если компрессор был
        # выключен при его старте: вместо сырой telnet-ошибки — понятное
        # «применится при перезапуске», и значения сохраняются в реестре.
        station_id, _ = self._make_running_liquidsoap_station()
        with mock.patch.object(panel, "_telnet_session",
                               return_value=["Syntax error or variable not found.\nEND"] * 5):
            res = self.client.post(
                "/api/streams/{}/adjust".format(station_id),
                json={"compressor": {"threshold": -30, "ratio": 5}})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertIn("перезапуске", data["message"])
        station = self.client.get("/api/streams/" + station_id).get_json()["station"]
        self.assertEqual(station["compressor"]["threshold"], -30.0)

    def test_adjust_sections_sends_telnet_and_persists(self):
        # Живое выключение секции: «var.set panel_<секция>_on = false» +
        # сохранение флага enabled в реестре (как у BYPASS)
        station_id, _ = self._make_running_liquidsoap_station()
        with mock.patch.object(panel, "_telnet_command",
                               return_value="Variable panel_comp_on set.\nEND") as tel:
            res = self.client.post(
                "/api/streams/{}/adjust".format(station_id),
                json={"sections": {"compressor": False}})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["mode"], "live")
        tel.assert_called_once_with("127.0.0.1", 55999, "var.set panel_comp_on = false")
        station = self.client.get("/api/streams/" + station_id).get_json()["station"]
        self.assertFalse(station["compressor"]["enabled"])
        # в ответе отдаются актуальные блоки обработки, включая loudnorm
        self.assertIn("loudnorm", data)

    def test_adjust_section_disabled_at_start_is_friendly(self):
        # Секция, выключенная при старте воркера, не имеет переменной
        # panel_<секция>_on: вместо ошибки — «включится при перезапуске»,
        # значение сохраняется в реестре
        station_id, _ = self._make_running_liquidsoap_station()
        with mock.patch.object(panel, "_telnet_command",
                               return_value="Syntax error or variable not found.\nEND"):
            res = self.client.post(
                "/api/streams/{}/adjust".format(station_id),
                json={"sections": {"loudnorm": True}})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertIn("перезапуске", data["message"])
        station = self.client.get("/api/streams/" + station_id).get_json()["station"]
        self.assertTrue(station["loudnorm"]["enabled"])

    def test_adjust_sections_validation(self):
        station_id, _ = self._make_running_liquidsoap_station()
        res = self.client.post(
            "/api/streams/{}/adjust".format(station_id),
            json={"sections": {"warp_drive": True}})
        self.assertEqual(res.status_code, 400)
        self.assertIn("неизвестные секции", res.get_json()["error"])
        res = self.client.post(
            "/api/streams/{}/adjust".format(station_id),
            json={"sections": ["compressor"]})
        self.assertEqual(res.status_code, 400)

    def test_listen_registration_replaces_same_channel(self):
        # Повторная прослушка того же канала станции гасит прежний
        # ffmpeg-монитор — иначе дубли копятся на каждый клик «Выход».
        class FakeProc:
            def __init__(self):
                self.killed = False

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                return 0

        try:
            p1, p2, p3 = FakeProc(), FakeProc(), FakeProc()
            panel.register_listener("station-1", p1, "output")
            panel.register_listener("station-1", p2, "output")
            self.assertTrue(p1.killed)   # дубликат канала заменён
            self.assertFalse(p2.killed)
            # другой канал той же станции живёт независимо
            panel.register_listener("station-1", p3, "input")
            self.assertFalse(p2.killed)
            self.assertFalse(p3.killed)
            panel.stop_listeners("station-1")
            self.assertTrue(p2.killed)
            self.assertTrue(p3.killed)
        finally:
            panel.stop_listeners("station-1")

    def test_listen_loopback_rewrite_in_container_mode(self):
        # Панель в контейнере: loopback-адреса источника и Icecast выхода в
        # подслушке указывают на loopback контейнера — переписываются на
        # host.docker.internal, как в скриптах воркеров.
        self.client.put("/api/streams/station-1", json={
            "outputs": [{"icecast": {"host": "127.0.0.1", "port": 8800,
                                     "mount": "/out.aacp"}}],
        })
        popen_patch, calls, procs = self.make_fake_popen()
        with popen_patch, mock.patch.object(panel, "_PANEL_INSIDE_CONTAINER", True):
            res = self.client.get("/api/streams/station-1/listen/output")
            self.assertEqual(res.status_code, 200)
            self.assertIn("http://host.docker.internal:8800/out.aacp", calls[-1])
            res.close()
            res = self.client.get("/api/streams/station-1/listen/input")
            self.assertEqual(res.status_code, 200)
            self.assertIn("http://host.docker.internal:8000/input", calls[-1])
            res.close()
        for proc in procs:
            proc.kill()
        panel.stop_listeners("station-1")

    def test_listen_host_gateway_rewrite_on_host_panel(self):
        # Панель на хосте: host.docker.internal из конфига недоступен —
        # слушаем loopback (прежнее поведение сохранено).
        self.client.put("/api/streams/station-1", json={
            "outputs": [{"icecast": {"host": "host.docker.internal", "port": 8800,
                                     "mount": "/out.aacp"}}],
        })
        popen_patch, calls, procs = self.make_fake_popen()
        with popen_patch, mock.patch.object(panel, "_PANEL_INSIDE_CONTAINER", False):
            res = self.client.get("/api/streams/station-1/listen/output")
            self.assertEqual(res.status_code, 200)
            self.assertIn("http://127.0.0.1:8800/out.aacp", calls[-1])
            res.close()
        for proc in procs:
            proc.kill()
        panel.stop_listeners("station-1")

    def test_bind_source_mapping_for_containerized_panel(self):        # Панель в контейнере: bind source резолвится демоном на хосте, поэтому
        # WORKER_BIND_ROOT переносит относительный путь скрипта на хостовый корень.
        with mock.patch.object(panel, "WORKER_BIND_ROOT",
                               r"D:\proj\data\runtime"):
            with mock.patch.object(panel, "RUNTIME_DIR", "/data/runtime"):
                src = panel.DockerWorkerManager._bind_source(
                    "/data/runtime/station-1/station.liq")
        self.assertEqual(src, os.path.join(r"D:\proj\data\runtime",
                                           "station-1", "station.liq"))
        # без WORKER_BIND_ROOT путь возвращается как есть (панель на хосте)
        with mock.patch.object(panel, "WORKER_BIND_ROOT", ""):
            self.assertEqual(
                panel.DockerWorkerManager._bind_source("/x/station.liq"),
                "/x/station.liq")

    def test_containerize_loopback_rewrites_icecast_and_source(self):
        # Воркер в контейнере: loopback там — сам контейнер. Icecast всех
        # выходов и источник на той же машине переписываются на
        # host.docker.internal, причём только в копии конфига (реестр не трогаем).
        cfg = {
            "source": {"url": "http://127.0.0.1:8000/live?x=1"},
            "outputs": [
                {"icecast": {"host": "localhost", "port": 8800, "mount": "/m"}},
                {"icecast": {"host": "127.0.0.1", "port": 8801, "mount": "/m2"}},
            ],
        }
        out = panel.containerize_loopback(cfg)
        self.assertEqual(out["outputs"][0]["icecast"]["host"], "host.docker.internal")
        self.assertEqual(out["outputs"][1]["icecast"]["host"], "host.docker.internal")
        self.assertEqual(out["source"]["url"],
                         "http://host.docker.internal:8000/live?x=1")
        # исходный конфиг не изменился
        self.assertEqual(cfg["outputs"][0]["icecast"]["host"], "localhost")
        self.assertEqual(cfg["source"]["url"], "http://127.0.0.1:8000/live?x=1")

    def test_containerize_loopback_keeps_external_hosts(self):
        cfg = {
            "source": {"url": "https://radio.example.com/stream.aacp"},
            "outputs": [{"icecast": {"host": "icecast.example-cdn.com", "port": 8443}}],
        }
        out = panel.containerize_loopback(cfg)
        self.assertEqual(out["outputs"][0]["icecast"]["host"], "icecast.example-cdn.com")
        self.assertEqual(out["source"]["url"], "https://radio.example.com/stream.aacp")

    def test_containerize_loopback_preserves_url_userinfo_and_port(self):
        cfg = {"source": {"url": "http://user:pw@localhost:9000/a"},
               "outputs": [{"icecast": {"host": "127.0.0.1"}}]}
        out = panel.containerize_loopback(cfg)
        self.assertEqual(out["source"]["url"],
                         "http://user:pw@host.docker.internal:9000/a")

    def test_containerize_loopback_rewrites_fallback_url(self):
        cfg = {"source": {"url": "http://127.0.0.1:8000/live"},
               "outputs": [{"icecast": {"host": "127.0.0.1"}}],
               "fallback": {"enabled": True, "mode": "url",
                            "url": "http://127.0.0.1:8800/backup.aacp"}}
        out = panel.containerize_loopback(cfg)
        self.assertEqual(out["fallback"]["url"],
                         "http://host.docker.internal:8800/backup.aacp")
        # внешний адрес заглушки не трогаем
        cfg["fallback"]["url"] = "https://cdn.example.com/backup.mp3"
        out = panel.containerize_loopback(cfg)
        self.assertEqual(out["fallback"]["url"], "https://cdn.example.com/backup.mp3")

    def test_analyzer_stream_url(self):
        # Анализатор всегда в контейнере: loopback из конфига — loopback
        # хоста, переписываем на host.docker.internal
        def out(host, port, mount, bitrate=128, out_id="out-1", enabled=True):
            return {"id": out_id, "enabled": enabled,
                    "encoder": {"codec": "aac", "bitrate": bitrate,
                                "sample_rate": 44100, "channels": 2},
                    "icecast": {"host": host, "port": port, "mount": mount,
                                "user": "source", "password": ""}}

        cfg = {"outputs": [out("127.0.0.1", 9000, "/test.aacp")]}
        self.assertEqual(panel._analyzer_stream_url(cfg),
                         "http://host.docker.internal:9000/test.aacp")
        cfg = {"outputs": [out("localhost", 8800, "live")]}
        self.assertEqual(panel._analyzer_stream_url(cfg),
                         "http://host.docker.internal:8800/live")
        # реальные адреса (LAN/интернет/host.docker.internal) не трогаем
        cfg = {"outputs": [out("192.168.0.223", 8800, "/test.aacp")]}
        self.assertEqual(panel._analyzer_stream_url(cfg),
                         "http://192.168.0.223:8800/test.aacp")
        cfg = {"outputs": [out("host.docker.internal", 9000, "/m")]}
        self.assertEqual(panel._analyzer_stream_url(cfg),
                         "http://host.docker.internal:9000/m")
        # авто-мониторинг — выход с самым высоким битрейтом среди включённых
        cfg = {"outputs": [out("10.0.0.1", 8000, "/hi", bitrate=256, out_id="hi"),
                          out("10.0.0.1", 8000, "/low", bitrate=96, out_id="low")]}
        self.assertEqual(panel._analyzer_stream_url(cfg), "http://10.0.0.1:8000/hi")
        # явный выбор мониторинга — monitor_output
        cfg["monitor_output"] = "low"
        self.assertEqual(panel._analyzer_stream_url(cfg), "http://10.0.0.1:8000/low")
        # все выходы выключены — анализировать нечего
        cfg = {"outputs": [out("10.0.0.1", 8000, "/hi", enabled=False)]}
        self.assertEqual(panel._analyzer_stream_url(cfg), "")

    def test_analyzer_container_name_sanitized(self):
        self.assertEqual(panel._analyzer_container_name("station-1"),
                         "audio-analyzer-station-1")
        # недопустимые для имени контейнера символы → дефис
        self.assertEqual(panel._analyzer_container_name("station 1/x"),
                         "audio-analyzer-station-1-x")

    def test_fallback_normalization(self):
        # неизвестный режим → silence; url-режим без адреса → silence
        st = self.client.put("/api/streams/station-1", json={
            "fallback": {"enabled": True, "mode": "loud_alarm", "url": ""},
        }).get_json()["station"]
        self.assertEqual(st["fallback"]["mode"], "silence")
        st = self.client.put("/api/streams/station-1", json={
            "fallback": {"enabled": True, "mode": "url", "url": "  "},
        }).get_json()["station"]
        self.assertEqual(st["fallback"]["mode"], "silence")
        # корректный url-режим сохраняется
        st = self.client.put("/api/streams/station-1", json={
            "fallback": {"enabled": True, "mode": "url",
                         "url": "http://backup:8000/live"},
        }).get_json()["station"]
        self.assertEqual(st["fallback"],
                         {"enabled": True, "mode": "url", "url": "http://backup:8000/live"})
        # по умолчанию резерв выключен
        fresh = self.client.post("/api/streams", json={"name": "Fresh"}).get_json()["station"]
        st = self.client.get("/api/streams/" + fresh["id"]).get_json()["station"]
        self.assertEqual(st["fallback"]["enabled"], False)
        self.assertEqual(st["fallback"]["mode"], "silence")

    def test_command_preview_shows_containerized_loopback(self):
        # предпросмотр скрипта совпадает с реальным: loopback переписан
        res = self.client.post("/api/streams", json={"name": "Liq"}).get_json()["station"]
        self.client.put("/api/streams/" + res["id"], json={
            "engine": "liquidsoap",
            "outputs": [{"icecast": {"host": "127.0.0.1", "port": 8800, "mount": "/t",
                                     "user": "source", "password": "pw"}}],
            "source": {"url": "http://127.0.0.1:8000/live"},
        })
        data = self.client.post("/api/streams/" + res["id"] + "/command",
                                json={}).get_json()
        self.assertIn('host="host.docker.internal"', data["text"])
        self.assertIn("http://host.docker.internal:8000/live", data["text"])
        self.assertNotIn("127.0.0.1", data["text"])
        # реестр при этом хранит адреса как задал пользователь
        station = self.client.get("/api/streams/" + res["id"]).get_json()["station"]
        self.assertEqual(station["outputs"][0]["icecast"]["host"], "127.0.0.1")
        self.assertEqual(station["source"]["url"], "http://127.0.0.1:8000/live")

    def test_adjust_limiter_clamps_and_persists(self):
        station_id, _ = self._make_running_liquidsoap_station()
        with mock.patch.object(panel, "_telnet_session",
                               return_value=["OK\nEND"] * 3) as sess:
            res = self.client.post(
                "/api/streams/{}/adjust".format(station_id),
                json={"limiter": {"ceiling_db": 5, "attack": 1, "release": 50}})
        self.assertEqual(res.status_code, 200)
        commands = sess.call_args[0][2]
        self.assertEqual(len(commands), 3)
        # потолок клампится к 0 дБ (верх границы)
        self.assertIn("var.set panel_lim_ceiling = 0.0", commands)
        station = self.client.get("/api/streams/" + station_id).get_json()["station"]
        self.assertTrue(station["limiter"]["enabled"])
        self.assertEqual(station["limiter"]["ceiling_db"], 0.0)

    def test_adjust_compressor_validation(self):
        station_id, _ = self._make_running_liquidsoap_station()
        res = self.client.post(
            "/api/streams/{}/adjust".format(station_id),
            json={"compressor": {"threshold": "громко"}})
        self.assertEqual(res.status_code, 400)
        res = self.client.post(
            "/api/streams/{}/adjust".format(station_id),
            json={"limiter": [-1, 5, 50]})
        self.assertEqual(res.status_code, 400)

    def test_outputs_crud_and_monitor_output(self):
        # Два выхода: нормализация полей, уникальные id, выбор мониторинга
        res = self.client.put("/api/streams/station-1", json={
            "outputs": [
                {"id": "hi",
                 "encoder": {"codec": "aac", "bitrate": 256, "sample_rate": 48000,
                             "channels": 2},
                 "icecast": {"host": "ice.example", "port": 8000, "mount": "/hi"}},
                {"id": "hi",
                 "encoder": {"codec": "mp3", "bitrate": 96, "sample_rate": 44100,
                             "channels": 1},
                 "icecast": {"host": "ice.example", "port": 8000, "mount": "/low.mp3"}},
            ],
        })
        self.assertEqual(res.status_code, 200)
        st = res.get_json()["station"]
        # дубль id выхода получил суффикс, поля нормализованы
        self.assertEqual([o["id"] for o in st["outputs"]], ["hi", "hi-2"])
        self.assertEqual(st["outputs"][1]["encoder"]["codec"], "mp3")
        self.assertEqual(st["outputs"][1]["encoder"]["channels"], 1)
        self.assertEqual(st["outputs"][0]["icecast"]["user"], "source")
        # авто-мониторинг — самый высокий битрейт среди включённых
        self.assertEqual(st["monitor_output"], "")
        self.assertEqual(panel.monitor_output(st)["id"], "hi")
        # явный выбор мониторинга сохраняется
        st = self.client.put("/api/streams/station-1", json={
            "monitor_output": "hi-2"}).get_json()["station"]
        self.assertEqual(st["monitor_output"], "hi-2")
        self.assertEqual(panel.monitor_output(st)["id"], "hi-2")
        # несуществующий выход сбрасывается в авто
        st = self.client.put("/api/streams/station-1", json={
            "monitor_output": "nope"}).get_json()["station"]
        self.assertEqual(st["monitor_output"], "")
        self.assertEqual(panel.monitor_output(st)["id"], "hi")

    def test_outputs_same_target_within_station_rejected(self):
        # два выхода одной станции на одну точку Icecast — ошибка
        res = self.client.put("/api/streams/station-1", json={
            "outputs": [
                {"icecast": {"host": "ice.example", "port": 8000, "mount": "/same"}},
                {"icecast": {"host": "ICE.example", "port": 8000, "mount": "/same"}},
            ]})
        self.assertEqual(res.status_code, 400)
        self.assertIn("одну точку", res.get_json()["error"])

    def test_legacy_station_encoder_icecast_migrate_to_outputs(self):
        # реестр старого формата (одиночные encoder/icecast) мигрирует в
        # outputs[0] при загрузке, верхнеуровневые ключи исчезают
        doc = {"settings": {}, "streams": [{
            "id": "old-1", "name": "Старая",
            "encoder": {"codec": "aac", "bitrate": 96, "sample_rate": 44100,
                        "channels": 2},
            "icecast": {"host": "ice.old", "port": 8000, "mount": "/old",
                        "user": "source", "password": "pw"},
        }]}
        Path(panel.STREAMS_PATH).write_text(json.dumps(doc), encoding="utf-8")
        station = self.client.get("/api/streams/old-1").get_json()["station"]
        self.assertNotIn("icecast", station)
        self.assertNotIn("encoder", station)
        self.assertEqual(len(station["outputs"]), 1)
        out = station["outputs"][0]
        self.assertEqual(out["encoder"]["bitrate"], 96)
        self.assertEqual(out["icecast"]["host"], "ice.old")
        self.assertEqual(out["icecast"]["mount"], "/old")
        self.assertEqual(out["icecast"]["password"], "pw")

    def test_process_isolation_and_autorestart(self):
        res = self.client.post("/api/streams", json={"name": "Радио 2"})
        station2_id = res.get_json()["station"]["id"]
        res = self.client.put("/api/streams/station-1", json={
            "autorestart": {"enabled": True, "delay": 1},
        })
        self.assertTrue(res.get_json()["ok"])

        popen_patch, client = self.make_fake_docker()
        with popen_patch:
            self.assertTrue(self.client.post("/api/streams/station-1/start").get_json()["ok"])
            self.assertTrue(self.client.post("/api/streams/" + station2_id + "/start").get_json()["ok"])
            self.assertEqual(len(client.containers.created), 2)

            status1 = self.client.get("/api/streams/station-1/status").get_json()
            status2 = self.client.get("/api/streams/" + station2_id + "/status").get_json()
            self.assertTrue(status1["running"])
            self.assertTrue(status2["running"])
            self.assertNotEqual(status1["pid"], status2["pid"])

            c1, c2 = client.containers.created
            self.assertTrue(self.client.post("/api/streams/station-1/stop").get_json()["ok"])
            self.assertTrue(c1.stopped)
            self.assertFalse(c2.stopped)
            self.assertTrue(self.client.get("/api/streams/" + station2_id + "/status").get_json()["running"])

            # Перезапускаем первую станцию и эмулируем падение контейнера.
            self.assertTrue(self.client.post("/api/streams/station-1/restart").get_json()["ok"])
            self.assertEqual(len(client.containers.created), 3)
            client.containers.created[2].die()

            deadline = time.time() + 8
            while time.time() < deadline:
                if len(client.containers.created) >= 4:
                    break
                time.sleep(0.05)
            self.assertGreaterEqual(len(client.containers.created), 4,
                                    "watchdog не перезапустил упавшую станцию")

            manager1 = panel.get_manager("station-1")
            manager2 = panel.get_manager(station2_id)
            deadline = time.time() + 3
            while time.time() < deadline and manager1.restarts < 1:
                time.sleep(0.05)
            self.assertGreaterEqual(manager1.restarts, 1)
            self.assertEqual(manager2.restarts, 0)
            self.assertFalse(c2.stopped, "соседняя станция не должна пострадать")

            logs = self.client.get("/api/streams/station-1/logs?since=0").get_json()["logs"]
            self.assertTrue(any("автоперезапуск выполнен" in item["line"] for item in logs))

    def test_delete_stops_only_selected_station(self):
        res = self.client.post("/api/streams", json={"name": "Радио 2"})
        station2_id = res.get_json()["station"]["id"]

        popen_patch, client = self.make_fake_docker()
        with popen_patch:
            self.client.post("/api/streams/station-1/start")
            self.client.post("/api/streams/" + station2_id + "/start")
            self.assertEqual(self.client.delete("/api/streams/station-1").status_code, 200)
            c1, c2 = client.containers.created
            self.assertTrue(c1.stopped)
            self.assertFalse(c2.stopped)

        data = self.client.get("/api/streams").get_json()
        self.assertEqual([s["id"] for s in data["streams"]], [station2_id])
        self.assertNotIn("station-1", panel._managers)

    def test_health_exposes_containerized_flag(self):
        with mock.patch.object(panel, "_PANEL_INSIDE_CONTAINER", False):
            res = self.client.get("/api/health").get_json()
            self.assertFalse(res["containerized"])
        with mock.patch.object(panel, "_PANEL_INSIDE_CONTAINER", True):
            res = self.client.get("/api/health").get_json()
            self.assertTrue(res["containerized"])

    def test_health_ffmpeg_available(self):
        # Путь задан и существует — ffmpeg доступен, поле в UI скрывается.
        self.client.put("/api/settings", json={"ffmpeg_path": str(self.fake_ffmpeg)})
        res = self.client.get("/api/health").get_json()
        self.assertTrue(res["ffmpeg_available"])
        # Путь задан, но не существует — недоступен, поле показывается.
        self.client.put("/api/settings",
                        json={"ffmpeg_path": str(self.tmp_path / "nope.exe")})
        res = self.client.get("/api/health").get_json()
        self.assertFalse(res["ffmpeg_available"])

    def test_delete_last_station_is_rejected(self):
        res = self.client.delete("/api/streams/station-1")
        self.assertEqual(res.status_code, 409)
        self.assertIn("последнюю", res.get_json()["error"])
        data = self.client.get("/api/streams").get_json()
        self.assertEqual([s["id"] for s in data["streams"]], ["station-1"])

    def test_container_mode_uses_volume_and_network(self):
        # Режим «панель в контейнере»: воркер в общей сети, скрипт с named
        # volume (read-only), порты на хост не публикуются.
        popen_patch, client = self.make_fake_docker()
        with popen_patch, \
             mock.patch.object(panel, "_PANEL_INSIDE_CONTAINER", True), \
             mock.patch.object(panel, "WORKER_NETWORK", "audiopanel_net"), \
             mock.patch.object(panel, "WORKER_VOLUME", "panel-data"):
            res = self.client.post("/api/streams/station-1/start")
            self.assertTrue(res.get_json()["ok"], res.get_json())
            self.assertEqual(client.networks.created, ["audiopanel_net"])
            container = client.containers.created[0]
            kwargs = container.create_kwargs
            self.assertEqual(kwargs.get("network"), "audiopanel_net")
            self.assertNotIn("ports", kwargs)
            mounts = kwargs.get("mounts") or []
            self.assertEqual(len(mounts), 1)
            self.assertEqual(mounts[0]["Type"], "volume")
            self.assertEqual(mounts[0]["Source"], "panel-data")
            self.assertTrue(mounts[0]["ReadOnly"])
            # Команда — путь скрипта на общем томе (как его видит панель).
            self.assertEqual(len(kwargs.get("command")), 1)
            self.assertTrue(kwargs["command"][0].endswith("station.liq"))
            # Telnet живой подстройки — по имени контейнера в общей сети.
            manager = panel.get_manager("station-1")
            self.assertEqual(manager.telnet_host_port(),
                             ("audio-worker-station-1", panel.liq.TELNET_PORT))

    def test_host_mode_uses_bind_mount_and_published_port(self):
        # Хостовый режим (панель не в контейнере) — прежняя схема: bind-mount
        # скрипта и telnet-порт на 127.0.0.1.
        popen_patch, client = self.make_fake_docker()
        with popen_patch, \
             mock.patch.object(panel, "_PANEL_INSIDE_CONTAINER", False):
            res = self.client.post("/api/streams/station-1/start")
            self.assertTrue(res.get_json()["ok"], res.get_json())
            self.assertEqual(client.networks.created, [])
            container = client.containers.created[0]
            kwargs = container.create_kwargs
            self.assertNotIn("network", kwargs)
            self.assertIn("ports", kwargs)
            mounts = kwargs.get("mounts") or []
            self.assertEqual(mounts[0]["Type"], "bind")
            self.assertEqual(mounts[0]["Target"], "/station.liq")
            self.assertEqual(kwargs.get("command"), ["/station.liq"])


if __name__ == "__main__":
    unittest.main()
