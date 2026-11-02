#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Генератор скрипта Liquidsoap из модели станции.

Панель (control plane) генерирует .liq из шаблона и параметров станции и
монтирует его в контейнер-воркер. Цепочка повторяет FFmpeg-этап:
EQ (10 полос) → усиление → компрессор → loudnorm (динамический) → лимитер,
затем AAC (FDK) → Icecast. Метаданные ICY идут сквозь конвейер автоматически.
"""

EQ_FREQS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]

# ffmpeg equalizer width_type: 0=h (Гц), 1=q, 2=o (октавы), 3=s (slope), 4=k (кГц)
EQ_WIDTH_TYPE_OCTAVE = 2


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


def _gain_call(cfg):
    gain = cfg.get("gain", {})
    if not gain.get("enabled"):
        return None
    db = float(gain.get("db", 0.0))
    if abs(db) <= 0.01:
        return None
    # volume-фильтр принимает строку с суффиксом dB
    text = "{:.2f}".format(db).rstrip("0").rstrip(".")
    sign = "+" if db > 0 else ""
    return 'ffmpeg.filter.volume(volume="{s}{v}dB", graph, '.format(s=sign, v=text)


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


def _processing_block(cfg):
    """Возвращает блок обработки (строка) или None для BYPASS."""
    if cfg.get("bypass"):
        return None

    calls = []
    calls += _eq_calls(cfg)
    gain = _gain_call(cfg)
    if gain:
        calls.append(gain)
    for stage in (_compressor_call(cfg), _loudnorm_call(cfg), _limiter_call(cfg)):
        if stage:
            calls.append(stage)

    if not calls:
        return None  # всё выключено — декод/код без фильтров

    # Граф собираем вложенными вызовами вокруг трека: let-переменные
    # Liquidsoap неизменяемы, поэтому цепочка — matryoshka из вызовов фильтров.
    expr = "a_in"
    for call in calls:
        expr = call + expr + ")"
    return (
        "# --- цепочка обработки: EQ → gain → comp → loudnorm → limiter ---\n"
        "# ffmpeg-фильтры работают поверх raw-формата, декодируем в него и обратно\n"
        "s = ffmpeg.raw.encode.audio(%ffmpeg(%audio.raw), s)\n"
        "\n"
        "def dsp_mkfilter(graph) =\n"
        "  let {audio = a_in} = source.tracks(s)\n"
        "  a_in = ffmpeg.filter.audio.input(graph, a_in)\n"
        "  a_in = " + expr + "\n"
        "  a_in = ffmpeg.filter.audio.output(graph, a_in)\n"
        "  source({audio = a_in, metadata = track.metadata(a_in), track_marks = track.track_marks(a_in)})\n"
        "end\n"
        "s = ffmpeg.filter.create(dsp_mkfilter)\n"
        "s = ffmpeg.raw.decode.audio(s)"
    )


def generate_liq_script(cfg):
    """Собирает полный .liq для одной станции. cfg — плоский runtime-конфиг."""
    station_id = str(cfg.get("id") or "station")
    name = str(cfg.get("name") or station_id)
    src = cfg.get("source", {})
    enc = cfg.get("encoder", {})
    ice = cfg.get("icecast", {})

    url = str(src.get("url") or "")
    block = _processing_block(cfg)

    parts = [
        "# Аудио-воркер Liquidsoap — файл сгенерирован панелью автоматически.",
        "# Станция: {sid}. Ручные правки будут перезаписаны при следующем старте.".format(sid=_liq_string(station_id)),
        "",
        'settings.log.level := 4',
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
