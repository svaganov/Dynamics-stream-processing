#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Генератор скрипта Liquidsoap из модели станции.

Панель (control plane) генерирует .liq из шаблона и параметров станции и
монтирует его в контейнер-воркер. Цепочка повторяет FFmpeg-этап:
EQ (10 полос) → усиление → компрессор → loudnorm (динамический) → лимитер,
затем AAC (FDK) → Icecast. Метаданные ICY идут сквозь конвейер автоматически.

Усиление вынесено из ffmpeg-графа в amplify(panel_gain(), s): panel_gain —
интерактивная переменная, поэтому панель может подстраивать громкость
работающего воркера без перезапуска (telnet: var.set panel_gain = <линейн.>).

Обработанный сигнал собирается в отдельную ветку proc, а выход воркера —
switch на интерактивной переменной panel_bypass: панель переключает
BYPASS работающего воркера живьём (telnet: var.set panel_bypass = true|false).
"""

EQ_FREQS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]

# ffmpeg equalizer width_type: 0=h (Гц), 1=q, 2=o (октавы), 3=s (slope), 4=k (кГц)
EQ_WIDTH_TYPE_OCTAVE = 2

# Telnet-сервер воркера внутри контейнера. Панель публикует порт на 127.0.0.1
# хоста со случайным портом и находит его через Docker API.
TELNET_PORT = 1234


def db_to_linear(db):
    return 10.0 ** (float(db) / 20.0)


def _f(value):
    """Форматирует float для Liquidsoap: без научной нотации, всегда с точкой.

    Без десятичной точки литерал считывается как int и не проходит типизацию
    (gain=2 — это int, а фильтры ждут float?)."""
    text = "{:.6f}".format(float(value))
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if "." not in text:
        text += ".0"
    return text


def _liq_string(text):
    """Экранирует строку для двойных кавычек Liquidsoap."""
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _eq_calls(cfg):
    eq = cfg.get("equalizer", {})
    if not eq.get("enabled"):
        return []
    calls = []
    for freq, gain in zip(EQ_FREQS, eq.get("gains", [])):
        if abs(float(gain)) <= 0.01:
            continue  # нулевые полосы не плодим, как и в FFmpeg-цепочке
        calls.append(
            "ffmpeg.filter.equalizer(f={f}, t={t}, w=1., g={g}, graph, ".format(
                f=_f(freq), t=EQ_WIDTH_TYPE_OCTAVE, g=_f(gain)
            )
        )
    return calls


def _compressor_call(cfg):
    comp = cfg.get("compressor", {})
    if not comp.get("enabled"):
        return None
    threshold = db_to_linear(comp.get("threshold", -20))  # ffmpeg-порог — линейный
    makeup = db_to_linear(comp.get("makeup", 0))          # и makeup тоже
    return (
        "ffmpeg.filter.acompressor(threshold={t}, ratio={r}, attack={a},"
        " release={rel}, makeup={m}, graph, ".format(
            t=_f(threshold), r=_f(comp.get("ratio", 4)),
            a=_f(comp.get("attack", 5)), rel=_f(comp.get("release", 120)),
            m=_f(makeup),
        )
    )


def _loudnorm_call(cfg):
    ln = cfg.get("loudnorm", {})
    if not ln.get("enabled"):
        return None
    # linear=false — динамический режим, пригодный для live-потоков
    return "ffmpeg.filter.loudnorm(i={i}, tp={tp}, lra={lra}, linear=false, graph, ".format(
        i=_f(ln.get("i", -14)), tp=_f(ln.get("tp", -1.5)), lra=_f(ln.get("lra", 11)),
    )


def _limiter_call(cfg):
    lim = cfg.get("limiter", {})
    if not lim.get("enabled"):
        return None
    ceiling = db_to_linear(lim.get("ceiling_db", -1.0))  # alimiter limit — линейный
    return "ffmpeg.filter.alimiter(limit={c}, attack={a}, release={r}, graph, ".format(
        c=_f(ceiling), a=_f(lim.get("attack", 5)), r=_f(lim.get("release", 50)),
    )


def _graph_block(fn_name, title, calls, src="s", dst="proc"):
    """Блок ffmpeg.filter.create из вложенных вызовов фильтров.

    let-переменные Liquidsoap неизменяемы, поэтому цепочка — matryoshka
    из вызовов фильтров вокруг a_in. Raw-обёртка источника ожидается снаружи."""
    if not calls:
        return None
    expr = "a_in"
    for call in calls:
        expr = call + expr + ")"
    lines = [
        "# --- {title} ---".format(title=title),
        "def {fn}(graph) =".format(fn=fn_name),
        "  let {{audio = a_in}} = source.tracks({src})".format(src=src),
        "  a_in = ffmpeg.filter.audio.input(graph, a_in)",
        "  a_in = " + expr,
        "  a_in = ffmpeg.filter.audio.output(graph, a_in)",
        "  source({audio = a_in, metadata = track.metadata(a_in), track_marks = track.track_marks(a_in)})",
        "end",
        "{dst} = ffmpeg.filter.create({fn})".format(dst=dst, fn=fn_name),
    ]
    return "\n".join(lines)


def _gain_block(cfg, src="proc", dst="proc"):
    """Статическое усиление через amplify с интерактивной переменной panel_gain.

    В отличие от ffmpeg-графа, значение amplify меняется живьём: панель шлёт
    по telnet «var.set panel_gain = <линейное>» — перезапуск не нужен."""
    gain = cfg.get("gain") or {}
    if not gain.get("enabled"):
        return None
    db = float(gain.get("db", 0.0))
    if abs(db) <= 0.01:
        return None
    return (
        "# --- усиление: amplify(panel_gain(), {src}) — живьём через telnet ---\n"
        "{dst} = amplify(panel_gain(), {src})".format(src=src, dst=dst)
    )


def _live_bypass_block():
    """Переключатель «чистый вход ↔ обработка» на интерактивной переменной.

    Панель шлёт по telnet «var.set panel_bypass = true|false» — воркер
    переключает ветку без перезапуска. buffer(proc) разводит clock-контекст:
    ffmpeg-графы живут в собственном вложенном clock, switch требует единый."""
    return (
        "# Live BYPASS: «var.set panel_bypass = true|false» по telnet — без перезапуска\n"
        'panel_bypass = interactive.bool("panel_bypass", false)\n'
        "s = switch(track_sensitive=false, [\n"
        "  (panel_bypass, s),\n"
        "  ({not panel_bypass()}, buffer(proc))\n"
        "])"
    )


def _processing_block(cfg):
    """Блок обработки + live-BYPASS, или None для статического BYPASS.

    Статический bypass (cfg["bypass"]) — ветки обработки в скрипте нет совсем.
    Иначе обработка собирается в переменную proc, а выход — switch на
    интерактивной переменной panel_bypass (панель переключает живьём)."""
    if cfg.get("bypass"):
        return None

    eq_calls = _eq_calls(cfg)
    gain_block = _gain_block(cfg)
    dyn_calls = []
    for stage in (_compressor_call(cfg), _loudnorm_call(cfg), _limiter_call(cfg)):
        if stage:
            dyn_calls.append(stage)

    if not (eq_calls or gain_block or dyn_calls):
        return None  # всё выключено — декод/код без фильтров, live-BYPASS не нужен

    parts = [
        "# --- цепочка обработки (ветка proc, live-BYPASS через panel_bypass): "
        "EQ → усиление → компрессор → loudnorm → лимитер ---",
    ]

    def _raw_wrapped(graph_block, comment, src, dst):
        # ffmpeg-фильтры работают поверх raw-формата: encode → граф → decode.
        # Каждый граф — со своей парой encode/decode: amplify требует PCM-источник
        # (raw-источник ему не подходит по типам), поэтому усиление живёт
        # в PCM-домене между двумя raw-графами.
        return "\n\n".join([
            "# raw-обёртка: {c}\n"
            "{dst} = ffmpeg.raw.encode.audio(%ffmpeg(%audio.raw), {src})".format(
                c=comment, src=src, dst=dst),
            graph_block,
            "{dst} = ffmpeg.raw.decode.audio({src})".format(src=dst, dst=dst),
        ])

    current = "s"
    if eq_calls:
        # encode пишет raw в proc — граф читает треки proc (raw), не current;
        # после decode proc снова PCM.
        eq_block = _graph_block("eq_mkfilter", "EQ (10 полос)", eq_calls,
                                src="proc", dst="proc")
        parts.append(_raw_wrapped(eq_block, "EQ-граф", current, "proc"))
        current = "proc"
    if gain_block:
        parts.append(_gain_block(cfg, src=current, dst="proc"))
        current = "proc"
    if dyn_calls:
        dyn_block = _graph_block("dyn_mkfilter", "компрессор → loudnorm → лимитер",
                                 dyn_calls, src="proc", dst="proc")
        parts.append(_raw_wrapped(dyn_block, "граф динамики", current, "proc"))
        current = "proc"

    parts.append(_live_bypass_block())
    return "\n\n".join(parts)


def _initial_gain_linear(cfg):
    """Стартовое линейное усиление интерактивной переменной из конфигурации."""
    gain = cfg.get("gain") or {}
    if gain.get("enabled"):
        db = float(gain.get("db", 0.0))
        if abs(db) > 0.01:
            return db_to_linear(db)
    return 1.0


def generate_liq_script(cfg):
    """Собирает полный .liq для одной станции. cfg — плоский runtime-конфиг."""
    station_id = str(cfg.get("id") or "station")
    name = str(cfg.get("name") or station_id)
    src = cfg.get("source", {})
    enc = cfg.get("encoder", {})
    ice = cfg.get("icecast", {})

    url = str(src.get("url") or "")
    block = _processing_block(cfg)
    gain_linear = _initial_gain_linear(cfg)

    parts = [
        "# Аудио-воркер Liquidsoap — файл сгенерирован панелью автоматически.",
        "# Станция: {sid}. Ручные правки будут перезаписаны при следующем старте.".format(sid=_liq_string(station_id)),
        "",
        'settings.log.level := 4',
        "",
        "# Telnet-сервер управления (панель подстраивает gain живьём)",
        "settings.server.telnet := true",
        "settings.server.telnet.port := {port}".format(port=int(TELNET_PORT)),
        # Внутри контейнера — на всех интерфейсах, иначе Docker не сможет
        # пробросить порт (default bind 127.0.0.1). Снаружи порт всё равно
        # публикуется только на 127.0.0.1 хоста (см. DockerWorkerManager).
        'settings.server.telnet.bind_addr := "0.0.0.0"',
        "",
        'source_url = "{url}"'.format(url=_liq_string(url)),
        "input_s = input.http(",
        '  id="input",',
        "  max_buffer=10.0,",
        "  poll_delay=1.0,",
        "  timeout=10.0,",
        '  user_agent="AudioPanel-Worker/2.0",',
        "  source_url",
        ")",
        "",
        "s = input_s",
        "",
        "# StreamTitle и прочие теги источника — в лог воркера (видно в панели)",
        "def panel_log_metadata(m) =",
        '  print("[metadata] " ^ json.stringify(m))',
        "end",
        "s.on_metadata(synchronous=true, panel_log_metadata)",
        "",
        "# Интерактивное усиление: живьём по telnet «var.set panel_gain = <линейное>»",
        'panel_gain = interactive.float("panel_gain", {v})'.format(v=_f(gain_linear)),
        "",
    ]

    if block:
        parts.append(block)
        parts.append("")

    parts += [
        "output.icecast(",
        '  id="icecast",',
        '  host="{host}",'.format(host=_liq_string(ice.get("host", "127.0.0.1"))),
        "  port={port},".format(port=int(ice.get("port", 8000))),
        '  user="{user}",'.format(user=_liq_string(ice.get("user", "source"))),
        '  password="{pw}",'.format(pw=_liq_string(ice.get("password", ""))),
        '  mount="{mount}",'.format(mount=_liq_string(str(ice.get("mount") or "/stream"))),
        '  name="{name}",'.format(name=_liq_string(name)),
        '  description="AudioPanel worker {sid}",'.format(sid=_liq_string(station_id)),
        "  fallible=true,",
        "  %fdkaac(channels={ch}, samplerate={sr}, bitrate={br}, afterburner=true),".format(
            ch=int(enc.get("channels", 2)), sr=int(enc.get("sample_rate", 44100)),
            br=int(enc.get("bitrate", 128)),
        ),
        "  s",
        ")",
        "",
    ]
    return "\n".join(parts)


def mask_liq_script(script, cfg):
    """Замаскированная для показа в UI копия скрипта (пароль Icecast)."""
    password = str(cfg.get("icecast", {}).get("password") or "")
    if password:
        script = script.replace('password="{}"'.format(password), 'password="***"')
    return script
