#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Генератор скрипта Liquidsoap из модели станции.

Панель (control plane) генерирует .liq из шаблона и параметров станции и
монтирует его в контейнер-воркер. Цепочка повторяет FFmpeg-этап:
EQ (10 полос) → усиление → компрессор → loudnorm (динамический) → лимитер,
затем AAC (FDK) → Icecast. Метаданные ICY идут сквозь конвейер автоматически.

Усиление — amplify(panel_gain, s): panel_gain — интерактивная переменная,
передаётся в amplify как геттер () -> float (саму функцию, не вызов —
иначе значение «запеклось» бы при старте), поэтому панель подстраивает
громкость работающего воркера без перезапуска (telnet: var.set panel_gain = <линейн.>).

Полосы EQ — нативные IIR-пики filter.iir.eq.peak с gain-геттерами
panel_eq_<частота> (ffmpeg-вариант принимал gain только статически и живьём
не менялся): любую полосу (в том числе с нуля) можно подстроить на
работающем воркере (telnet: var.set panel_eq_1000 = 3.0).

Компрессор и лимитер — нативные compress/limit в PCM-домене, все ключевые
параметры (порог, ratio, attack, release, makeup; ceiling, attack, release)
передаются геттерами от интерактивных переменных panel_comp_* / panel_lim_*:
панель подстраивает динамику работающего воркера без перезапуска
(telnet: var.set panel_comp_threshold = -24.). Раньше оба жили внутри
ffmpeg-графа и «запекались» при старте. Loudnorm нативного живого
эквивалента не имеет — остаётся единственным фильтром ffmpeg-графа,
его параметры применяются при перезапуске воркера.

Обработанный сигнал собирается в отдельную ветку proc, а выход воркера —
switch на интерактивной переменной panel_bypass: панель переключает
BYPASS работающего воркера живьём (telnet: var.set panel_bypass = true|false).
"""

EQ_FREQS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]

# Q IIR-пиковой полосы ≈ ширина 1 октава (аналог w=1. у бывшего
# ffmpeg.filter.equalizer): Q = sqrt(2^bw)/(2^bw - 1), bw=1 -> 1.4142.
EQ_PEAK_Q = 1.41

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


def eq_var_name(freq):
    """Имя интерактивной переменной полосы EQ (telnet: var.set panel_eq_<f>)."""
    return "panel_eq_{}".format(int(freq))


def _eq_chain(cfg, src="s", dst="proc"):
    """10-полосный EQ цепочкой нативных IIR-пиковых фильтров.

    ffmpeg.filter.equalizer внутри ffmpeg-графа принимает gain только как
    статический float — значения «запекались» при старте и живьём не менялись.
    Нативный filter.iir.eq.peak принимает gain-геттер () -> float и перечитывает
    его каждый кадр: полосы реально подстраиваются telnet-командой
    «var.set panel_eq_<f> = <дБ>» без перезапуска воркера. q=1.41 ≈ ширина 1 октава
    (та, что раньше задавалась w=1. у ffmpeg-equalizer)."""
    eq = cfg.get("equalizer", {})
    if not eq.get("enabled"):
        return None
    lines = ["# --- EQ (10 полос, нативные IIR-пики — gain живой) ---"]
    current = src
    for freq in EQ_FREQS:
        nxt = "eq_{}hz".format(freq)
        lines.append(
            "{dst} = filter.iir.eq.peak(frequency={f}, q={q}, gain={g}, {src})".format(
                dst=nxt, f=_f(float(freq)), q=_f(EQ_PEAK_Q),
                g=eq_var_name(freq), src=current))
        current = nxt
    if current != dst:
        lines.append("{dst} = {src}".format(dst=dst, src=current))
    return "\n".join(lines)


def _eq_interactive_vars(cfg):
    """Объявления panel_eq_<f> со стартовыми gains из конфигурации."""
    eq = cfg.get("equalizer", {})
    if not eq.get("enabled"):
        return None
    lines = ["# Полосы EQ (10) — живьём по telnet «var.set panel_eq_<f> = <дБ>»"]
    for freq, gain in zip(EQ_FREQS, eq.get("gains", [])):
        gain = max(-12.0, min(12.0, float(gain)))
        lines.append('{name} = interactive.float("{name}", {v})'.format(
            name=eq_var_name(freq), v=_f(gain)))
    return "\n".join(lines)


def _compressor_block(cfg, src="proc", dst="proc"):
    """Нативный компрессор compress с живыми параметрами-геттерами.

    ffmpeg.filter.acompressor принимал значения только статически — они
    «запекались» при генерации .liq и живьём не менялись. Нативный compress
    перечитывает геттеры каждый кадр: порог, ratio, attack, release и makeup
    подстраиваются telnet-командами «var.set panel_comp_* = ...» без
    перезапуска воркера. Makeup у compress — это gain (post-amplification, дБ)."""
    comp = cfg.get("compressor", {})
    if not comp.get("enabled"):
        return None
    return (
        "# --- компрессор: compress — threshold/ratio/attack/release/makeup живые ---\n"
        "# (параметры-геттеры panel_comp_*: compress перечитывает их каждый кадр)\n"
        "{dst} = compress(threshold=panel_comp_threshold, ratio=panel_comp_ratio,\n"
        "  attack=panel_comp_attack, release=panel_comp_release,\n"
        "  gain=panel_comp_makeup, {src})".format(src=src, dst=dst)
    )


def _loudnorm_call(cfg):
    ln = cfg.get("loudnorm", {})
    if not ln.get("enabled"):
        return None
    # linear=false — динамический режим, пригодный для live-потоков.
    # Loudnorm остался единственным фильтром ffmpeg-графа: нативного живого
    # эквивалента нет, параметры применяются при перезапуске воркера.
    return "ffmpeg.filter.loudnorm(i={i}, tp={tp}, lra={lra}, linear=false, graph, ".format(
        i=_f(ln.get("i", -14)), tp=_f(ln.get("tp", -1.5)), lra=_f(ln.get("lra", 11)),
    )


def _limiter_block(cfg, src="proc", dst="proc"):
    """Нативный лимитер limit с живыми параметрами-геттерами.

    Заменяет ffmpeg.filter.alimiter (level=false): потолок, attack и release
    перечитываются каждый кадр из panel_lim_* — панель подстраивает лимитер
    работающего воркера без перезапуска. ceiling (дБ) у limit — это threshold."""
    lim = cfg.get("limiter", {})
    if not lim.get("enabled"):
        return None
    return (
        "# --- лимитер: limit — ceiling/attack/release живые ---\n"
        "# (параметры-геттеры panel_lim_*: limit перечитывает их каждый кадр)\n"
        "{dst} = limit(threshold=panel_lim_ceiling, attack=panel_lim_attack,\n"
        "  release=panel_lim_release, {src})".format(src=src, dst=dst)
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
    # Блок включаем всегда, когда усиление включено — даже при 0 дБ:
    # иначе живое управление panel_gain пропадёт из цепочки.
    return (
        "# --- усиление: amplify(panel_gain, {src}) — живьём через telnet ---\n"
        "# (передаём саму функцию () -> float: amplify перечитывает её каждый кадр;\n"
        "#  вызов panel_gain() «запек» бы значение на момент старта скрипта)\n"
        "{dst} = amplify(panel_gain, {src})".format(src=src, dst=dst)
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

    eq_chain = _eq_chain(cfg)
    gain_block = _gain_block(cfg)
    comp_block = _compressor_block(cfg)
    ln_call = _loudnorm_call(cfg)
    lim_block = _limiter_block(cfg)

    if not (eq_chain or gain_block or comp_block or ln_call or lim_block):
        return None  # всё выключено — декод/код без фильтров, live-BYPASS не нужен

    parts = [
        "# --- цепочка обработки (ветка proc, live-BYPASS через panel_bypass): "
        "EQ → усиление → компрессор → loudnorm → лимитер ---",
    ]

    def _raw_wrapped(graph_block, comment, src, dst):
        # Loudnorm — единственный оставшийся ffmpeg-фильтр: raw encode → граф →
        # decode. buffer() после decode разводит clock-контекст (граф живёт
        # во вложенном clock), чтобы за ним мог идти нативный лимитер.
        return "\n\n".join([
            "# raw-обёртка: {c}\n"
            "{dst} = ffmpeg.raw.encode.audio(%ffmpeg(%audio.raw), {src})".format(
                c=comment, src=src, dst=dst),
            graph_block,
            "{dst} = ffmpeg.raw.decode.audio({src})".format(src=dst, dst=dst),
            "{dst} = buffer({src})".format(src=dst, dst=dst),
        ])

    current = "s"
    if eq_chain:
        parts.append(_eq_chain(cfg, src=current, dst="proc"))
        current = "proc"
    if gain_block:
        parts.append(_gain_block(cfg, src=current, dst="proc"))
        current = "proc"
    if comp_block:
        parts.append(_compressor_block(cfg, src=current, dst="proc"))
        current = "proc"
    if ln_call:
        ln_graph = _graph_block("loudnorm_mkfilter", "loudnorm (ffmpeg-граф)",
                                [ln_call], src="proc", dst="proc")
        parts.append(_raw_wrapped(ln_graph, "граф loudnorm", current, "proc"))
        current = "proc"
    if lim_block:
        parts.append(_limiter_block(cfg, src=current, dst="proc"))
        current = "proc"

    parts.append(_live_bypass_block())
    return "\n\n".join(parts)


def _dyn_interactive_vars(cfg):
    """Объявления panel_comp_* / panel_lim_* со стартовыми значениями из конфигурации.

    Объявляются только для включённых блоков: лишняя переменная не мешает,
    но чистый скрипт проще читать в панели."""
    lines = []
    comp = cfg.get("compressor", {})
    if comp.get("enabled"):
        lines.append("# Компрессор (живьём по telnet «var.set panel_comp_<парам> = <знач>»)")
        for name, value in (
            ("panel_comp_threshold", max(-60.0, min(0.0, float(comp.get("threshold", -20))))),
            ("panel_comp_ratio", max(1.0, min(20.0, float(comp.get("ratio", 4))))),
            ("panel_comp_attack", max(0.1, min(1000.0, float(comp.get("attack", 5))))),
            ("panel_comp_release", max(10.0, min(2000.0, float(comp.get("release", 120))))),
            ("panel_comp_makeup", max(-24.0, min(24.0, float(comp.get("makeup", 0))))),
        ):
            lines.append('{name} = interactive.float("{name}", {v})'.format(
                name=name, v=_f(value)))
    lim = cfg.get("limiter", {})
    if lim.get("enabled"):
        lines.append("# Лимитер (живьём по telnet «var.set panel_lim_<парам> = <знач>»)")
        for name, value in (
            ("panel_lim_ceiling", max(-30.0, min(0.0, float(lim.get("ceiling_db", -1))))),
            ("panel_lim_attack", max(0.1, min(1000.0, float(lim.get("attack", 5))))),
            ("panel_lim_release", max(10.0, min(2000.0, float(lim.get("release", 50))))),
        ):
            lines.append('{name} = interactive.float("{name}", {v})'.format(
                name=name, v=_f(value)))
    return "\n".join(lines) if lines else None


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

    eq_vars = _eq_interactive_vars(cfg)
    if eq_vars:
        parts += [eq_vars, ""]

    dyn_vars = _dyn_interactive_vars(cfg)
    if dyn_vars:
        parts += [dyn_vars, ""]

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
