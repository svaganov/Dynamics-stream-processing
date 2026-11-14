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
        # порядок: EQ (нативные IIR-пики, PCM) → amplify (PCM) → compress (PCM,
        # живые геттеры) → raw-обёртка loudnorm (единственный ffmpeg-фильтр) →
        # buffer (развод clock) → limit (PCM, живые геттеры); ветка proc,
        # выход — switch на panel_bypass
        pos_eq = script.index("filter.iir.eq.peak(frequency=31")
        pos_amp = script.index("gain_stage = amplify(panel_gain, s)")
        pos_comp = script.index("comp_stage = compress(threshold=panel_comp_threshold")
        pos_raw = script.index("ln_raw = ffmpeg.raw.encode.audio")
        pos_ln = script.index("ffmpeg.filter.loudnorm")
        pos_dec = script.index("ln_stage = ffmpeg.raw.decode.audio")
        pos_buf = script.index("ln_stage = buffer(ln_stage)")
        pos_lim = script.index("lim_stage = limit(threshold=panel_lim_ceiling")
        pos_proc = script.index("proc = s")
        self.assertEqual(script.count("ffmpeg.raw.encode.audio"), 1)
        self.assertEqual(script.count("ffmpeg.raw.decode.audio"), 1)
        self.assertLess(pos_eq, pos_amp)
        self.assertLess(pos_amp, pos_comp)
        self.assertLess(pos_comp, pos_raw)
        self.assertLess(pos_raw, pos_ln)
        self.assertLess(pos_ln, pos_dec)
        self.assertLess(pos_dec, pos_buf)
        self.assertLess(pos_buf, pos_lim)
        self.assertLess(pos_lim, pos_proc)
        # живые выключатели секций: bool-переменная на каждую включённую секцию
        for var in ("panel_eq_on", "panel_gain_on", "panel_comp_on",
                    "panel_ln_on", "panel_lim_on"):
            self.assertIn('{0} = interactive.bool("{0}", true)'.format(var), script)
        # каждая секция — свой switch: 5 секций + финальный BYPASS
        self.assertEqual(script.count("s = switch(track_sensitive=false, ["), 6)
        self.assertIn("(panel_comp_on, comp_stage)", script)
        self.assertIn("({not panel_comp_on()}, s)", script)
        # live-BYPASS: интерактивная переменная + switch «вход ↔ обработка»;
        # ветка «чистый вход» — raw_input (сырой вход до цепочки), а НЕ s:
        # s к этому месту уже переприсвоен секционными switch-ами (регрессия)
        self.assertIn('panel_bypass = interactive.bool("panel_bypass", false)', script)
        pos_switch = script.index("(panel_bypass, raw_input)")
        self.assertLess(pos_proc, pos_switch)
        self.assertIn("({not panel_bypass()}, buffer(proc))", script)
        # raw_input захватывается ДО первого секционного switch
        pos_raw = script.index("raw_input = s")
        self.assertLess(pos_raw, script.index("s = switch(track_sensitive=false, ["))
        # динамика больше НЕ в ffmpeg-графе: acompressor/alimiter заменены
        # нативными compress/limit с геттерами
        self.assertNotIn("ffmpeg.filter.acompressor", script)
        self.assertNotIn("ffmpeg.filter.alimiter", script)
        self.assertIn("gain=panel_comp_makeup", script)
        # loudnorm — динамический режим ffmpeg-графа
        self.assertIn("linear=false", script)
        # стартовые значения интерактивных переменных динамики из конфигурации
        self.assertIn('panel_comp_threshold = interactive.float("panel_comp_threshold", -20.0)', script)
        self.assertIn('panel_comp_ratio = interactive.float("panel_comp_ratio", 4.0)', script)
        self.assertIn('panel_comp_makeup = interactive.float("panel_comp_makeup", 6.0)', script)
        self.assertIn('panel_lim_ceiling = interactive.float("panel_lim_ceiling", -1.0)', script)
        self.assertIn('panel_lim_release = interactive.float("panel_lim_release", 50.0)', script)
        # выход на Icecast с FDK-AAC
        self.assertIn('host="ice.example"', script)
        self.assertIn("mount=\"/radio7\"", script)
        self.assertIn("%fdkaac(channels=2, samplerate=44100, bitrate=128", script)

    def test_comp_limit_without_loudnorm_has_no_raw_graph(self):
        # компрессор + лимитер нативные (PCM-домен): без loudnorm ffmpeg-граф
        # и raw-обёртка не нужны вовсе
        cfg = make_cfg(loudnorm={"enabled": False})
        script = liq.generate_liq_script(cfg)
        self.assertNotIn("ffmpeg.filter", script)
        self.assertNotIn("ffmpeg.raw", script)
        self.assertIn("compress(threshold=panel_comp_threshold", script)
        self.assertIn("limit(threshold=panel_lim_ceiling", script)

    def test_compressor_only_chain(self):
        cfg = make_cfg(equalizer={"enabled": False, "gains": [0] * 10},
                       gain={"enabled": False, "db": 0},
                       loudnorm={"enabled": False},
                       limiter={"enabled": False})
        script = liq.generate_liq_script(cfg)
        self.assertIn("comp_stage = compress(threshold=panel_comp_threshold", script)
        self.assertNotIn("lim_stage", script)
        self.assertNotIn("ffmpeg.filter", script)

    def test_limiter_only_chain(self):
        cfg = make_cfg(equalizer={"enabled": False, "gains": [0] * 10},
                       gain={"enabled": False, "db": 0},
                       compressor={"enabled": False},
                       loudnorm={"enabled": False})
        script = liq.generate_liq_script(cfg)
        self.assertIn("lim_stage = limit(threshold=panel_lim_ceiling", script)
        self.assertNotIn("compress(", script)
        self.assertNotIn("panel_comp_", script)
        self.assertNotIn("ffmpeg.filter", script)

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
        # Регрессия «значение запеклось при старте»: amplify, IIR-пики EQ,
        # compress и limit принимают геттеры () -> float — передаём саму функцию,
        # не вызов. Вызов panel_gain() «запек» бы значение на момент старта
        # воркера, и живые подстройки telnet'ом перестали бы доходить до звука.
        script = liq.generate_liq_script(make_cfg())
        self.assertIn("amplify(panel_gain, ", script)
        self.assertNotIn("amplify(panel_gain(), ", script)
        self.assertIn("gain=panel_eq_31,", script)
        self.assertNotIn("panel_eq_31()", script)
        # динамика: параметры-геттеры, не вызовы
        self.assertIn("threshold=panel_comp_threshold", script)
        self.assertIn("ratio=panel_comp_ratio", script)
        self.assertIn("gain=panel_comp_makeup", script)
        self.assertIn("threshold=panel_lim_ceiling", script)
        for name in ("panel_comp_threshold", "panel_comp_ratio",
                     "panel_comp_attack", "panel_comp_release",
                     "panel_comp_makeup", "panel_lim_ceiling",
                     "panel_lim_attack", "panel_lim_release"):
            self.assertNotIn(name + "(", script)

    def test_eq_disabled_has_no_interactive_bands(self):
        cfg = make_cfg(equalizer={"enabled": False, "gains": [1] * 10})
        script = liq.generate_liq_script(cfg)
        self.assertNotIn("filter.iir.eq.peak", script)
        self.assertNotIn("panel_eq_", script)

    def test_disabled_section_has_no_live_toggle(self):
        # выключенная при старте секция не получает ни bool-переменной, ни
        # switch-а — живое включение возможно только для секций, заявленных
        # в конфигурации воркера (как и живые параметры динамики)
        cfg = make_cfg(equalizer={"enabled": False, "gains": [1] * 10})
        script = liq.generate_liq_script(cfg)
        self.assertNotIn("panel_eq_on", script)
        self.assertNotIn("eq_stage", script)
        self.assertNotIn("(panel_eq_on, eq_stage)", script)
        # остальные включённые секции — со своими выключателями
        for var in ("panel_gain_on", "panel_comp_on", "panel_ln_on", "panel_lim_on"):
            self.assertIn('{0} = interactive.bool("{0}", true)'.format(var), script)
        # переключателей на одну меньше: 4 секции + BYPASS
        self.assertEqual(script.count("s = switch(track_sensitive=false, ["), 5)

    def test_bypass_has_no_filters(self):
        script = liq.generate_liq_script(make_cfg(bypass=True))
        self.assertNotIn("ffmpeg.filter", script)
        self.assertNotIn("ffmpeg.raw", script)
        self.assertNotIn("amplify", script)
        # статический BYPASS — без ветки proc и без live-переключателя
        self.assertNotIn("panel_bypass", script)
        self.assertNotIn("switch(", script)
        self.assertIn("output.icecast", script)

    def test_icecast_connected_flag_in_output(self):
        # флаг подключения выхода: интерактивная bool + колбэки on_connect/
        # on_disconnect пишут в её ref — панель читает «var.get panel_ice_connected»
        script = liq.generate_liq_script(make_cfg())
        self.assertIn('interactive.bool("panel_ice_connected", false)', script)
        self.assertIn('interactive.bool.ref("panel_ice_connected")', script)
        self.assertIn("on_connect={ice_connected_ref := true}", script)
        self.assertIn("on_disconnect={ice_connected_ref := false}", script)

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
        self.assertIn("gain_stage = amplify(panel_gain, s)", script)
        self.assertNotIn("ffmpeg.filter.create", script)
        self.assertNotIn("ffmpeg.raw", script)
        # даже одиночное усиление оборачивается live-BYPASS
        self.assertIn('panel_bypass = interactive.bool("panel_bypass", false)', script)
        self.assertIn("(panel_bypass, raw_input)", script)
        self.assertIn("({not panel_bypass()}, buffer(proc))", script)

    def test_zero_gain_stays_live_controllable(self):
        # gain включён при 0 дБ: блок усиления обязан остаться в цепочке,
        # иначе живое управление panel_gain пропало бы целиком
        cfg = make_cfg(gain={"enabled": True, "db": 0})
        script = liq.generate_liq_script(cfg)
        self.assertIn("gain_stage = amplify(panel_gain, s)", script)
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
