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
        self.assertEqual(station2["icecast"]["mount"], "/station-2")

        res = self.client.post("/api/streams", json={"id": "station-2", "name": "Дубликат"})
        self.assertEqual(res.status_code, 409)

        res = self.client.put(
            "/api/streams/station-2",
            json={"icecast": {"mount": "/station-1"}},
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
        self.assertEqual(station["icecast"]["mount"], "/stream")
        self.assertEqual(data["settings"]["ffmpeg_path"], "C:/ffmpeg/bin/ffmpeg.exe")

        flat = self.client.get("/api/config").get_json()
        self.assertEqual(flat["source"]["url"], "http://127.0.0.1:8000/input")
        self.assertEqual(flat["ffmpeg_path"], "C:/ffmpeg/bin/ffmpeg.exe")

    def test_command_masks_icecast_password(self):
        res = self.client.post("/api/streams/station-1/command", json={
            "icecast": {"password": "super-secret"},
        })
        self.assertEqual(res.status_code, 200)
        text = res.get_json()["text"]
        self.assertIn("source:***@127.0.0.1:8000", text)
        self.assertNotIn("super-secret", text)

    def test_gain_filter_in_ffmpeg_chain(self):
        res = self.client.post("/api/streams/station-1/command", json={
            "equalizer": {"enabled": True, "gains": [3] + [0] * 9},
            "gain": {"enabled": True, "db": 2.5},
            "compressor": {"enabled": True},
        })
        text = res.get_json()["text"]
        pos_eq = text.index("equalizer=f=31")
        pos_vol = text.index("volume=2.5dB")
        pos_comp = text.index("acompressor=")
        self.assertLess(pos_eq, pos_vol)
        self.assertLess(pos_vol, pos_comp)

        # выключенное/нулевое усиление не порождает фильтр
        res = self.client.post("/api/streams/station-1/command", json={
            "gain": {"enabled": False, "db": 0},
        })
        self.assertNotIn("volume=", res.get_json()["text"])

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

    def test_adjust_rejected_for_ffmpeg_engine(self):
        res = self.client.post("/api/streams/station-1/adjust", json={"gain_db": 3.0})
        self.assertEqual(res.status_code, 409)
        self.assertIn("liquidsoap", res.get_json()["error"].lower())

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

    def test_bypass_rejected_for_ffmpeg_engine(self):
        res = self.client.post("/api/streams/station-1/bypass", json={"enabled": True})
        self.assertEqual(res.status_code, 409)
        self.assertIn("liquidsoap", res.get_json()["error"].lower())

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

    def test_bind_source_mapping_for_containerized_panel(self):
        # Панель в контейнере: bind source резолвится демоном на хосте, поэтому
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

    def test_process_isolation_and_autorestart(self):
        res = self.client.post("/api/streams", json={"name": "Радио 2"})
        station2_id = res.get_json()["station"]["id"]
        res = self.client.put("/api/settings", json={"ffmpeg_path": str(self.fake_ffmpeg)})
        self.assertTrue(res.get_json()["ok"])
        res = self.client.put("/api/streams/station-1", json={
            "autorestart": {"enabled": True, "delay": 1},
        })
        self.assertTrue(res.get_json()["ok"])

        popen_patch, calls, procs = self.make_fake_popen()
        with popen_patch:
            def stream_items():
                # Вызовы ffmpeg -version из статуса не являются аудиопайплайнами.
                return [(c, p) for c, p in zip(calls, procs) if len(c) > 1 and c[1] != "-version"]

            self.assertTrue(self.client.post("/api/streams/station-1/start").get_json()["ok"])
            self.assertTrue(self.client.post("/api/streams/" + station2_id + "/start").get_json()["ok"])
            self.assertEqual(len(stream_items()), 2)

            status1 = self.client.get("/api/streams/station-1/status").get_json()
            status2 = self.client.get("/api/streams/" + station2_id + "/status").get_json()
            self.assertTrue(status1["running"])
            self.assertTrue(status2["running"])
            self.assertNotEqual(status1["pid"], status2["pid"])

            self.assertTrue(self.client.post("/api/streams/station-1/stop").get_json()["ok"])
            self.assertTrue(stream_items()[0][1].terminated)
            self.assertFalse(stream_items()[1][1].terminated)
            self.assertTrue(self.client.get("/api/streams/" + station2_id + "/status").get_json()["running"])

            # Перезапускаем первую станцию и эмулируем падение процесса.
            self.assertTrue(self.client.post("/api/streams/station-1/restart").get_json()["ok"])
            self.assertEqual(len(stream_items()), 3)
            stream_items()[2][1].die()

            deadline = time.time() + 5
            while time.time() < deadline:
                if len(stream_items()) >= 4:
                    break
                time.sleep(0.05)
            self.assertGreaterEqual(len(stream_items()), 4, "watchdog не перезапустил упавшую станцию")

            manager1 = panel.get_manager("station-1")
            manager2 = panel.get_manager(station2_id)
            deadline = time.time() + 3
            while time.time() < deadline and manager1.restarts < 1:
                time.sleep(0.05)
            self.assertGreaterEqual(manager1.restarts, 1)
            self.assertEqual(manager2.restarts, 0)
            self.assertFalse(stream_items()[1][1].terminated, "соседняя станция не должна пострадать")

            logs = self.client.get("/api/streams/station-1/logs?since=0").get_json()["logs"]
            self.assertTrue(any("автоперезапуск выполнен" in item["line"] for item in logs))

    def test_delete_stops_only_selected_station(self):
        res = self.client.post("/api/streams", json={"name": "Радио 2"})
        station2_id = res.get_json()["station"]["id"]
        self.client.put("/api/settings", json={"ffmpeg_path": str(self.fake_ffmpeg)})

        popen_patch, calls, procs = self.make_fake_popen()
        with popen_patch:
            self.client.post("/api/streams/station-1/start")
            self.client.post("/api/streams/" + station2_id + "/start")
            self.assertEqual(self.client.delete("/api/streams/station-1").status_code, 200)
            self.assertTrue(procs[0].terminated)
            self.assertFalse(procs[1].terminated)

        data = self.client.get("/api/streams").get_json()
        self.assertEqual([s["id"] for s in data["streams"]], [station2_id])
        self.assertNotIn("station-1", panel._managers)

    def test_delete_last_station_is_rejected(self):
        res = self.client.delete("/api/streams/station-1")
        self.assertEqual(res.status_code, 409)
        self.assertIn("последнюю", res.get_json()["error"])
        data = self.client.get("/api/streams").get_json()
        self.assertEqual([s["id"] for s in data["streams"]], ["station-1"])


if __name__ == "__main__":
    unittest.main()
