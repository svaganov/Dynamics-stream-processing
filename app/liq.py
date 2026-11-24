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

Включение/выключение секций (EQ, усиление, компрессор, loudnorm, лимитер)
работает живьём, как BYPASS: каждая включённая при старте секция обёрнута
switch на интерактивной bool-переменной panel_<секция>_on
(telnet: var.set panel_comp_on = false). Секция, выключенная при старте
воркера, появится в цепочке (и станет управляемой) после его перезапуска.

Обработанный сигнал собирается в отдельную ветку proc, а выход воркера —
switch на интерактивной переменной panel_bypass: панель переключает
BYPASS работающего воркера живьём (telnet: var.set panel_bypass = true|false).
"""

import re

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
    переключает ветку без перезапуска. Ветка «чистый вход» — raw_input
    (сырой вход до цепочки; важно: s к этому месту уже переприсвоен
    секционными switch-ами и равен обработанному сигналу). buffer(proc)
    разводит clock-контекст: ffmpeg-графы живут в собственном вложенном
    clock, switch требует единый. buffer на ветке raw_input не нужен —
    сырой вход уже в главном clock."""
    return (
        "# Live BYPASS: «var.set panel_bypass = true|false» по telnet — без перезапуска\n"
        'panel_bypass = interactive.bool("panel_bypass", false)\n'
        "s = switch(track_sensitive=false, [\n"
        "  (panel_bypass, raw_input),\n"
        "  ({not panel_bypass()}, buffer(proc))\n"
        "])"
    )


# Живые выключатели секций: переменная panel_<секция>_on объявляется в скрипте,
# только если секция включена в конфигурации станции (т.е. присутствует в
# цепочке). «var.set panel_<секция>_on = true|false» включает/выключает
# секцию работающего воркера без перезапуска — как BYPASS, но по секциям.
SECTION_TOGGLE_VARS = {
    "equalizer": "panel_eq_on",
    "gain": "panel_gain_on",
    "compressor": "panel_comp_on",
    "loudnorm": "panel_ln_on",
    "limiter": "panel_lim_on",
}


def _section_switch(var_name, stage_var, title):
    """switch «секция ↔ обход» на интерактивной bool-переменной.

    Ветка обхода — исходный s (до секции); ветка обработки — stage_var.
    Выход снова присваивается s: top-level rebinding в Liquidsoap допустим."""
    return (
        "# {title}: живой вкл/выкл секции — «var.set {var} = true|false» (без перезапуска)\n"
        "s = switch(track_sensitive=false, [\n"
        "  ({var}, {stage}),\n"
        "  ({{not {var}()}}, s)\n"
        "])".format(var=var_name, stage=stage_var, title=title)
    )


def _processing_block(cfg):
    """Блок обработки + live-BYPASS, или None для статического BYPASS.

    Статический bypass (cfg["bypass"]) — ветки обработки в скрипте нет совсем.
    Иначе каждая включённая секция собирается в отдельную переменную
    (<секция>_stage) и оборачивается switch на panel_<секция>_on: панель
    включает/выключает секции работающего воркера живьём (как BYPASS).
    Результат собирается в proc, выход — switch на panel_bypass."""
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
        "# --- цепочка обработки (live-BYPASS через panel_bypass, живые "
        "выключатели секций panel_*_on): EQ → усиление → компрессор → "
        "loudnorm → лимитер ---\n"
        "# Сырой вход до цепочки — для ветки BYPASS (s дальше переприсваивается "
        "секционными switch-ами и уже не годится как «чистый вход»)",
        "raw_input = s",
    ]

    def _raw_wrapped(graph_block, comment, src, dst):
        # Loudnorm — единственный оставшийся ffmpeg-фильтр: raw encode → граф →
        # decode. buffer() после decode разводит clock-контекст (граф живёт
        # во вложенном clock) перед возвратом в общую цепочку.
        return "\n\n".join([
            "# raw-обёртка: {c}\n"
            "ln_raw = ffmpeg.raw.encode.audio(%ffmpeg(%audio.raw), {src})".format(
                c=comment, src=src),
            graph_block,
            "{dst} = ffmpeg.raw.decode.audio(ln_raw)".format(dst=dst),
            "{dst} = buffer({dst})".format(dst=dst),
        ])

    if eq_chain:
        parts.append(_eq_chain(cfg, src="s", dst="eq_stage"))
        parts.append(_section_switch("panel_eq_on", "eq_stage", "EQ (10 полос)"))
    if gain_block:
        parts.append(_gain_block(cfg, src="s", dst="gain_stage"))
        parts.append(_section_switch("panel_gain_on", "gain_stage", "усиление"))
    if comp_block:
        parts.append(_compressor_block(cfg, src="s", dst="comp_stage"))
        parts.append(_section_switch("panel_comp_on", "comp_stage", "компрессор"))
    if ln_call:
        ln_graph = _graph_block("loudnorm_mkfilter", "loudnorm (ffmpeg-граф)",
                                [ln_call], src="ln_raw", dst="ln_raw")
        parts.append(_raw_wrapped(ln_graph, "граф loudnorm", "s", "ln_stage"))
        parts.append(_section_switch("panel_ln_on", "ln_stage", "loudnorm"))
    if lim_block:
        parts.append(_limiter_block(cfg, src="s", dst="lim_stage"))
        parts.append(_section_switch("panel_lim_on", "lim_stage", "лимитер"))

    parts.append("proc = s")
    parts.append(_live_bypass_block())
    return "\n\n".join(parts)


def _section_bool_vars(cfg):
    """Объявления panel_*_on для секций, включённых в конфигурации станции.

    Секция присутствует в цепочке воркера (и управляется живьём), только если
    была включена при старте; выключенные при старте секции появятся после
    перезапуска воркера."""
    mapping = (
        ("equalizer", "panel_eq_on"),
        ("gain", "panel_gain_on"),
        ("compressor", "panel_comp_on"),
        ("loudnorm", "panel_ln_on"),
        ("limiter", "panel_lim_on"),
    )
    lines = ["# Живые выключатели секций: «var.set panel_<секция>_on = true|false»"]
    for section, var in mapping:
        enabled = (cfg.get(section) or {}).get("enabled")
        if enabled:
            lines.append('{name} = interactive.bool("{name}", true)'.format(name=var))
    return "\n".join(lines) if len(lines) > 1 else None


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


def _encoder_format(enc):
    """Строка формата энкодера Liquidsoap для выхода (fdkaac / lame mp3)."""
    codec = str(enc.get("codec") or "aac").lower()
    sr = int(enc.get("sample_rate", 44100))
    br = int(enc.get("bitrate", 128))
    ch = int(enc.get("channels", 2))
    if codec == "mp3":
        return "%mp3(bitrate={br}, samplerate={sr}, stereo={st})".format(
            br=br, sr=sr, st="true" if ch == 2 else "false")
    return ("%fdkaac(channels={ch}, samplerate={sr}, bitrate={br}, "
            "afterburner=true)").format(ch=ch, sr=sr, br=br)


def _output_sections(cfg, station_id, station_name):
    """N выходов станции: output.icecast на каждый включённый выход.

    У каждого выхода свой флаг подключения panel_ice_connected_<id> —
    панель читает его живьём по telnet (статус выходов по отдельности)."""
    outputs = [o for o in (cfg.get("outputs") or []) if isinstance(o, dict)
               and o.get("enabled")]
    if not outputs:
        return ("\n".join([
            "# Все выходы выключены: фиктивный сток, чтобы тракт жил",
            "output.dummy(fallible=true, s)",
            "",
        ]), [])

    parts = []
    flag_names = []
    for out in outputs:
        oid = re.sub(r"[^A-Za-z0-9_]", "_", str(out.get("id") or "out"))
        enc = out.get("encoder") or {}
        ice = out.get("icecast") or {}
        flag = "panel_ice_connected_{}".format(oid)
        flag_names.append(flag)
        out_name = str(out.get("name") or "").strip() or station_name
        parts += [
            '# --- Выход «{oid}»: {codec} {br}k -> {host}:{port}{mount} ---'.format(
                oid=oid, codec=str(enc.get("codec") or "aac").upper(),
                br=int(enc.get("bitrate", 128)),
                host=str(ice.get("host", "?")), port=int(ice.get("port", 8000)),
                mount=str(ice.get("mount") or "/stream")),
            '{flag} = interactive.bool("{flag}", false)'.format(flag=flag),
            '{flag}_ref = interactive.bool.ref("{flag}")'.format(flag=flag),
            "output.icecast(",
            '  id="icecast_{oid}",'.format(oid=oid),
            '  host="{host}",'.format(host=_liq_string(ice.get("host", "127.0.0.1"))),
            "  port={port},".format(port=int(ice.get("port", 8000))),
            '  user="{user}",'.format(user=_liq_string(ice.get("user", "source"))),
            '  password="{pw}",'.format(pw=_liq_string(ice.get("password", ""))),
            '  mount="{mount}",'.format(mount=_liq_string(str(ice.get("mount") or "/stream"))),
            '  name="{name}",'.format(name=_liq_string(out_name)),
            '  description="AudioPanel worker {sid}",'.format(sid=_liq_string(station_id)),
            "  fallible=true,",
            "  on_connect={{{flag}_ref := true}},".format(flag=flag),
            "  on_disconnect={{{flag}_ref := false}},".format(flag=flag),
            "  {},".format(_encoder_format(enc)),
            "  s",
            ")",
            "",
        ]
    return "\n".join(parts), flag_names


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

    sec_vars = _section_bool_vars(cfg)
    if sec_vars:
        parts += [sec_vars, ""]

    if block:
        parts.append(block)
        parts.append("")

    fb = cfg.get("fallback") or {}
    if fb.get("enabled"):
        mode = str(fb.get("mode") or "silence")
        if mode == "tone":
            stub = ("# резерв: тихий тон 880 Гц ≈ −26 дБ — «техническая пауза»\n"
                    "stub = amplify(0.05, sine(880.))")
        elif mode == "url" and str(fb.get("url") or "").strip():
            stub = ("# резерв: запасной поток/файл (бесконечный источник)\n"
                    'stub = buffer(single("{url}"))'.format(
                        url=_liq_string(str(fb["url"]).strip())))
        else:
            stub = ("# резерв: тишина — эфир и слушатели не отваливаются\n"
                    "stub = blank()")
        parts += [
            "# --- резерв при обрыве входа: fallback переключает на заглушку, "
            "при возврате источника — обратно автоматически ---",
            stub,
            "s = fallback(track_sensitive=false, [s, stub])",
            "",
        ]

    parts += [
        # Живой StreamTitle: Liquidsoap (input.http на базе ffmpeg) не читает
        # in-stream ICY-блоки источника, поэтому панельный релей
        # metadata_relay вставляет тег через telnet «panel.set_title
        # <url-encoded>», а output.icecast рассылает его слушателям штатным
        # ICY-обновлением — работает с любой версией Icecast, в отличие от
        # admin API (Icecast 2.5 кладёт updinfo в x_icy_title, не в эфир).
        "# Команда «panel.set_title <url-encoded>»: вставка StreamTitle "
        "от панельного релея metadata_relay",
        "def panel_set_title(t) =",
        '  s.insert_metadata(new_track=false, [("title", url.decode(t))])',
        '  "ok"',
        "end",
        'server.register(namespace="panel", '
        'description="Set StreamTitle (url-encoded)", "set_title", panel_set_title)',
        "",
    ]
    # Выходы станции (каждый со своим кодированием и точкой Icecast) +
    # их флаги подключения panel_ice_connected_<id>.
    outputs_block, _flag_names = _output_sections(cfg, station_id, name)
    parts.append("# Выходы станции: у каждого свой флаг подключения — панель "
                 "читает их живьём по telnet «var.get panel_ice_connected_<id>»")
    parts.append(outputs_block)
    return "\n".join(parts)


def mask_liq_script(script, cfg):
    """Замаскированная для показа в UI копия скрипта (пароли Icecast выходов)."""
    for out in cfg.get("outputs") or []:
        if not isinstance(out, dict):
            continue
        password = str((out.get("icecast") or {}).get("password") or "")
        if password:
            script = script.replace('password="{}"'.format(password), 'password="***"')
    return script
