"""Тесты генератора Liquidsoap-скриптов и диспетчеризации движков."""

import copy
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = PROJECT_ROOT / "app" / "app.py"

spec = importlib.util.spec_from_file_location("panel_app_liq", APP_PATH)
panel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(panel)

sys.path.insert(0, str(PROJECT_ROOT / "app"))
import liq  # noqa: E402


def make_cfg(**overrides):
    cfg = {
        "id": "station-7",
        "name": "Радио 7",
        "bypass": False,
        "source": {"url": "http://host:8000/live", "reconnect": True, "reconnect_delay_max": 5},
        "equalizer": {"enabled": True, "gains": [3, 0, 1, 0, 0, 0, 0, 0, 2, 0]},
        "gain": {"enabled": True, "db": 2.5},
        "compressor": {"enabled": True, "threshold": -20, "ratio": 4, "attack": 5, "release": 120, "makeup": 6},
        "loudnorm": {"enabled": True, "i": -14, "tp": -1.5, "lra": 11},
        "limiter": {"enabled": True, "ceiling_db": -1.0, "attack": 5, "release": 50},
        "encoder": {"codec": "aac", "bitrate": 128, "sample_rate": 44100, "channels": 2},
        "icecast": {"host": "ice.example", "port": 8000, "mount": "/radio7",
                    "user": "source", "password": "s3cret"},
    }
    cfg.update(overrides)
    return cfg


class LiqGeneratorTest(unittest.TestCase):
    def test_full_chain_order_and_params(self):
        script = liq.generate_liq_script(make_cfg())
        # telnet-сервер управления поднят, усиление — интерактивная переменная
        self.assertIn("settings.server.telnet := true", script)
        self.assertIn("settings.server.telnet.port := 1234", script)
        # bind внутри контейнера — 0.0.0.0, иначе Docker не пробросит порт
        self.assertIn('settings.server.telnet.bind_addr := "0.0.0.0"', script)
        # 2.5 дБ -> линейное усиление 10 ** (2.5/20) = 1.333521
        self.assertIn('panel_gain = interactive.float("panel_gain", 1.333521)', script)
        # порядок: EQ (нативные IIR-пики, PCM) → amplify (PCM) → граф динамики
        # (raw-обёртка одна — только ffmpeg-фильтры); ветка proc, выход — switch
        pos_eq = script.index("filter.iir.eq.peak(frequency=31")
        pos_amp = script.index("proc = amplify(panel_gain, proc)")
        pos_raw = script.index("ffmpeg.raw.encode.audio")
        pos_comp = script.index("ffmpeg.filter.acompressor")
        pos_dec = script.rindex("ffmpeg.raw.decode.audio")
        self.assertEqual(script.count("ffmpeg.raw.encode.audio"), 1)
        self.assertEqual(script.count("ffmpeg.raw.decode.audio"), 1)
        self.assertLess(pos_eq, pos_amp)
        self.assertLess(pos_amp, pos_raw)
        self.assertLess(pos_raw, pos_comp)
        self.assertLess(pos_comp, pos_dec)
        # live-BYPASS: интерактивная переменная + switch «вход ↔ обработка»
        self.assertIn('panel_bypass = interactive.bool("panel_bypass", false)', script)
        pos_switch = script.index("s = switch(track_sensitive=false, [")
        self.assertLess(pos_dec, pos_switch)
        self.assertIn("(panel_bypass, s)", script)
        self.assertIn("({not panel_bypass()}, buffer(proc))", script)
        # пороги/makeup переведены из dB в линейные
        self.assertIn("threshold=0.1", script)
        self.assertIn("makeup=1.995262", script)
        self.assertIn("limit=0.891251", script)
        # loudnorm — динамический режим; у лимитера авто-подъём отключен
        self.assertIn("linear=false", script)
        self.assertIn("level=false, graph", script)
        # выход на Icecast с FDK-AAC
        self.assertIn('host="ice.example"', script)
        self.assertIn("mount=\"/radio7\"", script)
        self.assertIn("%fdkaac(channels=2, samplerate=44100, bitrate=128", script)

    def test_all_eq_bands_interactive(self):
        script = liq.generate_liq_script(make_cfg())
        # все 10 полос — нативные IIR-пики с живым gain-геттером, включая нулевые
        self.assertEqual(script.count("filter.iir.eq.peak"), 10)
        self.assertIn("gain=panel_eq_31", script)
        self.assertIn("gain=panel_eq_16000", script)
        self.assertNotIn("panel_eq_31()", script)  # вызов «запекал» бы значение
        # стартовые значения объявлены для всех полос
        self.assertIn('panel_eq_31 = interactive.float("panel_eq_31", 3.0)', script)
        self.assertIn('panel_eq_62 = interactive.float("panel_eq_62", 0.0)', script)
        self.assertIn('panel_eq_1000 = interactive.float("panel_eq_1000", 0.0)', script)

    def test_live_params_passed_as_getters_not_calls(self):
        # Регрессия «значение запеклось при старте»: amplify и IIR-пики EQ
        # принимают геттеры () -> float — передаём саму функцию, не вызов.
        # Вызов panel_gain() «запек» бы значение на момент старта воркера,
        # и живые подстройки telnet'ом перестали бы доходить до звука.
        script = liq.generate_liq_script(make_cfg())
        self.assertIn("amplify(panel_gain, ", script)
        self.assertNotIn("amplify(panel_gain(), ", script)
        self.assertIn("gain=panel_eq_31,", script)
        self.assertNotIn("panel_eq_31()", script)

    def test_eq_disabled_has_no_interactive_bands(self):
        cfg = make_cfg(equalizer={"enabled": False, "gains": [1] * 10})
        script = liq.generate_liq_script(cfg)
        self.assertNotIn("filter.iir.eq.peak", script)
        self.assertNotIn("panel_eq_", script)

    def test_bypass_has_no_filters(self):
        script = liq.generate_liq_script(make_cfg(bypass=True))
        self.assertNotIn("ffmpeg.filter", script)
        self.assertNotIn("ffmpeg.raw", script)
        self.assertNotIn("amplify", script)
        # статический BYPASS — без ветки proc и без live-переключателя
        self.assertNotIn("panel_bypass", script)
        self.assertNotIn("switch(", script)
        self.assertIn("output.icecast", script)

    def test_all_disabled_equals_no_filter_block(self):
        cfg = make_cfg(equalizer={"enabled": False, "gains": [0] * 10},
                       gain={"enabled": False, "db": 0},
                       compressor={"enabled": False},
                       loudnorm={"enabled": False},
                       limiter={"enabled": False})
        script = liq.generate_liq_script(cfg)
        self.assertNotIn("ffmpeg.filter.create", script)
        self.assertNotIn("amplify", script)
        # обработки нет — live-переключатель и полосы EQ не нужны
        self.assertNotIn("panel_bypass", script)
        self.assertNotIn("panel_eq_", script)
        # интерактивная переменная всё равно регистрируется (стартовое 1.0)
        self.assertIn('interactive.float("panel_gain", 1.0)', script)

    def test_gain_only_uses_amplify_without_raw_graph(self):
        cfg = make_cfg(equalizer={"enabled": False, "gains": [0] * 10},
                       compressor={"enabled": False},
                       loudnorm={"enabled": False},
                       limiter={"enabled": False})
        script = liq.generate_liq_script(cfg)
        self.assertIn("proc = amplify(panel_gain, s)", script)
        self.assertNotIn("ffmpeg.filter.create", script)
        self.assertNotIn("ffmpeg.raw", script)
        # даже одиночное усиление оборачивается live-BYPASS
        self.assertIn('panel_bypass = interactive.bool("panel_bypass", false)', script)
        self.assertIn("({not panel_bypass()}, buffer(proc))", script)

    def test_zero_gain_stays_live_controllable(self):
        # gain включён при 0 дБ: блок усиления обязан остаться в цепочке,
        # иначе живое управление panel_gain пропало бы целиком
        cfg = make_cfg(gain={"enabled": True, "db": 0})
        script = liq.generate_liq_script(cfg)
        self.assertIn("proc = amplify(panel_gain, proc)", script)
        self.assertIn('interactive.float("panel_gain", 1.0)', script)

    def test_password_masked(self):
        cfg = make_cfg()
        script = liq.generate_liq_script(cfg)
        masked = liq.mask_liq_script(script, cfg)
        self.assertNotIn("s3cret", masked)
        self.assertIn('password="***"', masked)
        self.assertIn("s3cret", script)  # в рабочем скрипте пароль настоящий

    def test_strings_escaped(self):
        cfg = make_cfg(source={"url": 'http://h/"q"', "reconnect": True, "reconnect_delay_max": 5},
                       name='Радио "7"')
        script = liq.generate_liq_script(cfg)
        self.assertIn('source_url = "http://h/\\"q\\""', script)


class EngineDispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        panel.STREAMS_PATH = str(self.tmp_path / "streams.json")
        panel.LEGACY_CONFIG_PATH = str(self.tmp_path / "config.json")
        panel.RUNTIME_DIR = str(self.tmp_path / "runtime")
        panel._managers.clear()
        panel.app.config["TESTING"] = True
        self.client = panel.app.test_client()

    def tearDown(self):
        panel._managers.clear()
        self.tmp.cleanup()

    def test_engine_normalized_and_dispatched(self):
        station = self.client.post("/api/streams", json={"name": "Liq"}).get_json()["station"]
        self.assertEqual(station["engine"], "ffmpeg")

        updated = self.client.put("/api/streams/" + station["id"], json={
            "engine": "liquidsoap",
            "gain": {"enabled": True, "db": 40},
        }).get_json()["station"]
        self.assertEqual(updated["engine"], "liquidsoap")
        self.assertEqual(updated["gain"]["db"], 24.0)  # кламп диапазона

        manager = panel.get_manager(station["id"])
        self.assertIsInstance(manager, panel.DockerWorkerManager)
        status = manager.status()
        self.assertEqual(status["engine"], "liquidsoap")
        self.assertFalse(status["running"])

        # обратная смена движка пересоздаёт менеджер
        self.client.put("/api/streams/" + station["id"], json={"engine": "ffmpeg"})
        manager = panel.get_manager(station["id"])
        self.assertIsInstance(manager, panel.ProcessManager)

    def test_command_endpoint_returns_liq_for_liquidsoap(self):
        station = self.client.post("/api/streams", json={"name": "Liq"}).get_json()["station"]
        self.client.put("/api/streams/" + station["id"], json={
            "engine": "liquidsoap",
            "equalizer": {"enabled": True, "gains": [1] + [0] * 9},
        })
        data = self.client.post("/api/streams/" + station["id"] + "/command",
                                json={}).get_json()
        self.assertEqual(data["cmd"], [])
        self.assertIn("filter.iir.eq.peak", data["text"])
        self.assertNotIn('"hackme"', data["text"])


if __name__ == "__main__":
    unittest.main()
