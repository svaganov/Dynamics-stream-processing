"use strict";

const $ = (id) => document.getElementById(id);

/* ---------- tabs ---------- */

function activateTab(name) {
  const tab = document.querySelector('.nav-tab[data-tab="' + name + '"]');
  if (!tab) return;
  document.querySelectorAll(".nav-tab").forEach((t) => {
    t.classList.toggle("active", t === tab);
    t.setAttribute("aria-selected", t === tab ? "true" : "false");
  });
  document.querySelectorAll(".tab-page").forEach((p) => {
    p.classList.toggle("active", p.id === "tab-" + tab.dataset.tab);
  });
}

document.querySelectorAll(".nav-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    activateTab(tab.dataset.tab);
    history.replaceState(null, "", "#" + tab.dataset.tab);
  });
});
if (location.hash) activateTab(location.hash.slice(1));

/* ---------- multi-station state ---------- */

let streamsDoc = { settings: { ffmpeg_path: "" }, streams: [] };
let stationMap = new Map();
let drafts = new Map();
let currentStationId = null;
let healthById = new Map();
let logCursors = new Map();
let lastHealth = { counts: { running: 0, stopped: 0 }, stations: [] };
let currentRunning = false; // работает ли выбранная станция (последний poll)

function selectedStation() {
  return currentStationId ? stationMap.get(currentStationId) || null : null;
}

function selectedDraftOrStation() {
  if (!currentStationId) return null;
  return drafts.get(currentStationId) || stationMap.get(currentStationId) || null;
}

function rememberCurrentDraft() {
  if (!currentStationId || !stationMap.has(currentStationId)) return;
  drafts.set(currentStationId, collectConfig());
}

/* ---------- equalizer ---------- */

const EQ_FREQS = [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000];
const EQ_LABELS = ["31", "62", "125", "250", "500", "1k", "2k", "4k", "8k", "16k"];
const EQ_PRESETS = {
  flat:   [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
  voice:  [-6, -4, -2, 0, 1, 2, 3, 2, 0, -2],
  music:  [3, 2, 1, 0, -1, -1, 0, 2, 3, 2],
  bass:   [6, 5, 4, 2, 1, 0, 0, 0, 0, 0],
  bright: [-1, -1, 0, 0, 0, 0, 1, 3, 5, 5],
};

function buildEqBands() {
  const row = $("eqRow");
  EQ_FREQS.forEach((freq, i) => {
    const band = document.createElement("div");
    band.className = "eq-band";

    const val = document.createElement("span");
    val.className = "eq-val";
    val.id = "eq_val_" + i;
    val.textContent = "0";

    const sliderWrap = document.createElement("div");
    sliderWrap.className = "eq-slider";
    const slider = document.createElement("input");
    slider.type = "range";
    slider.id = "eq_b" + i;
    slider.min = -12; slider.max = 12; slider.step = 0.5; slider.value = 0;
    slider.setAttribute("aria-label", "Полоса " + EQ_LABELS[i] + " Гц");
    slider.addEventListener("input", () => { syncEqLabels(); refreshCommand(); });
    slider.addEventListener("dblclick", () => { slider.value = 0; syncEqLabels(); refreshCommand(); });
    const zero = document.createElement("div");
    zero.className = "eq-zero";
    sliderWrap.appendChild(slider);
    sliderWrap.appendChild(zero);

    const lab = document.createElement("span");
    lab.className = "eq-freq";
    lab.textContent = EQ_LABELS[i];

    band.appendChild(val);
    band.appendChild(sliderWrap);
    band.appendChild(lab);
    row.appendChild(band);
  });
}

function syncEqLabels() {
  for (let i = 0; i < EQ_FREQS.length; i++) {
    const v = parseFloat($("eq_b" + i).value);
    $("eq_val_" + i).textContent = (v > 0 ? "+" : "") + v;
    $("eq_val_" + i).style.color = v === 0 ? "var(--text-dim)" : "var(--accent)";
  }
}

function applyEqPreset(name) {
  const gains = EQ_PRESETS[name];
  if (!gains) return;
  gains.forEach((g, i) => { $("eq_b" + i).value = g; });
  syncEqLabels();
  refreshCommand();
}

$("eq_preset").addEventListener("change", (e) => {
  if (e.target.value) {
    applyEqPreset(e.target.value);
    $("eq_enabled").checked = true;
    syncCardState();
  }
});

$("btnEqReset").addEventListener("click", () => {
  applyEqPreset("flat");
  $("eq_preset").value = "";
});

/* ---------- config <-> form ---------- */

const FIELDS = {
  bypass:         ["bypass", "bool"],
  engine:         ["engine"],
  src_url:        ["source", "url"],
  src_reconnect:  ["source", "reconnect", "bool"],
  src_reconnect_delay_max: ["source", "reconnect_delay_max", "num"],
  eq_enabled:     ["equalizer", "enabled", "bool"],
  gain_enabled:   ["gain", "enabled", "bool"],
  gain_db:        ["gain", "db", "num"],
  comp_enabled:   ["compressor", "enabled", "bool"],
  comp_threshold: ["compressor", "threshold", "num"],
  comp_ratio:     ["compressor", "ratio", "num"],
  comp_attack:    ["compressor", "attack", "num"],
  comp_release:   ["compressor", "release", "num"],
  comp_makeup:    ["compressor", "makeup", "num"],
  ln_enabled:     ["loudnorm", "enabled", "bool"],
  ln_i:           ["loudnorm", "i", "num"],
  ln_tp:          ["loudnorm", "tp", "num"],
  ln_lra:         ["loudnorm", "lra", "num"],
  lim_enabled:    ["limiter", "enabled", "bool"],
  lim_ceiling_db: ["limiter", "ceiling_db", "num"],
  lim_attack:     ["limiter", "attack", "num"],
  lim_release:    ["limiter", "release", "num"],
  enc_codec:      ["encoder", "codec"],
  enc_bitrate:    ["encoder", "bitrate", "num"],
  enc_sample_rate:["encoder", "sample_rate", "num"],
  enc_channels:   ["encoder", "channels", "num"],
  ice_host:       ["icecast", "host"],
  ice_port:       ["icecast", "port", "num"],
  ice_mount:      ["icecast", "mount"],
  ice_user:       ["icecast", "user"],
  ice_password:   ["icecast", "password"],
  ffmpeg_path:    ["ffmpeg_path"],
  ar_enabled:     ["autorestart", "enabled", "bool"],
  ar_delay:       ["autorestart", "delay", "num"],
};

function collectConfig() {
  const station = selectedStation() || {};
  const cfg = {
    id: currentStationId || station.id || "",
    name: station.name || currentStationId || "Станция",
    enabled: station.enabled !== false,
  };

  for (const [id, path] of Object.entries(FIELDS)) {
    const el = $(id);
    let value = el.type === "checkbox" ? el.checked : el.value;
    if (path.includes("num")) value = parseFloat(value);
    let node = cfg;
    const keys = path.filter((k) => k !== "num" && k !== "bool");
    for (let i = 0; i < keys.length - 1; i++) {
      node[keys[i]] = node[keys[i]] || {};
      node = node[keys[i]];
    }
    node[keys[keys.length - 1]] = value;
  }
  cfg.equalizer.gains = EQ_FREQS.map((_, i) => parseFloat($("eq_b" + i).value));
  return cfg;
}

function fillForm(cfg) {
  for (const [id, path] of Object.entries(FIELDS)) {
    const keys = path.filter((k) => k !== "num" && k !== "bool");
    let node = cfg;
    for (const k of keys) node = node ? node[k] : undefined;
    if (node === undefined) continue;
    const el = $(id);
    if (el.type === "checkbox") el.checked = Boolean(node);
    else el.value = node;
  }
  const gains = (cfg.equalizer && cfg.equalizer.gains) || [];
  gains.forEach((g, i) => { if (i < EQ_FREQS.length) $("eq_b" + i).value = g; });
  syncEqLabels();
  syncLabels();
  syncCardState();
}

/* ---------- labels & disabled sections ---------- */

const LABEL_FMT = {
  gain_db: (v) => (parseFloat(v) > 0 ? "+" : "") + v + " dB",
  comp_threshold: (v) => v + " dB",
  comp_ratio: (v) => v + " : 1",
  comp_attack: (v) => v + " мс",
  comp_release: (v) => v + " мс",
  comp_makeup: (v) => "+" + v + " dB",
  ln_i: (v) => v + " LUFS",
  ln_tp: (v) => v + " dBTP",
  ln_lra: (v) => v + " LU",
  lim_ceiling_db: (v) => v + " dBTP",
  lim_attack: (v) => v + " мс",
  lim_release: (v) => v + " мс",
  src_reconnect_delay_max: (v) => v + " с",
  ar_delay: (v) => v + " с",
};

function syncLabels() {
  for (const [id, fmt] of Object.entries(LABEL_FMT)) {
    const target = $(id + "_val");
    if (target) target.textContent = fmt($(id).value);
  }
}

function syncCardState() {
  const bypass = $("bypass").checked;
  $("bypassToggle").classList.toggle("on", bypass);
  $("cardEqualizer").classList.toggle("disabled", bypass);
  $("eqBandsWrap").classList.toggle("disabled", bypass || !$("eq_enabled").checked);
  $("cardCompressor").classList.toggle("disabled", bypass || !$("comp_enabled").checked);
  $("cardLoudnorm").classList.toggle("disabled", bypass || !$("ln_enabled").checked);
  $("cardLimiter").classList.toggle("disabled", bypass || !$("lim_enabled").checked);
  syncGainLive();
}

/* ---------- right panel: chain + urls ---------- */

function syncChain() {
  const cfg = collectConfig();
  $("rpInput").textContent = cfg.source.url || "—";
  $("rpOutput").textContent =
    "icecast://" + cfg.icecast.host + ":" + cfg.icecast.port + "/" +
    String(cfg.icecast.mount).replace(/^\/+/, "");

  const chain = $("rpChain");
  chain.innerHTML = "";
  if (cfg.bypass) {
    const chip = document.createElement("span");
    chip.className = "chip bypass";
    chip.textContent = "BYPASS — ОБХОД";
    chain.appendChild(chip);
    return;
  }
  const steps = [
    { name: "EQ", on: cfg.equalizer.enabled && cfg.equalizer.gains.some((g) => g !== 0) },
    { name: "GAIN", on: cfg.gain.enabled && parseFloat(cfg.gain.db) !== 0 },
    { name: "COMP", on: cfg.compressor.enabled },
    { name: "LUFS", on: cfg.loudnorm.enabled },
    { name: "LIM", on: cfg.limiter.enabled },
  ];
  steps.forEach((s, i) => {
    if (i > 0) {
      const arr = document.createElement("span");
      arr.className = "arr";
      arr.textContent = "→";
      chain.appendChild(arr);
    }
    const chip = document.createElement("span");
    chip.className = "chip" + (s.on ? " on" : "");
    chip.textContent = s.name;
    chain.appendChild(chip);
  });
}

/* ---------- command preview ---------- */

let cmdTimer = null;
function refreshCommand() {
  clearTimeout(cmdTimer);
  cmdTimer = setTimeout(async () => {
    if (!currentStationId) return;
    try {
      const res = await fetch("/api/streams/" + encodeURIComponent(currentStationId) + "/command", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(collectConfig()),
      });
      const data = await res.json();
      if (data.text) $("cmdBox").textContent = data.text;
    } catch (e) { /* ignore */ }
    syncChain();
  }, 150);
}

/* ---------- toast ---------- */

let toastTimer = null;
function toast(msg, isError) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.toggle("error", Boolean(isError));
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

/* ---------- station list ---------- */

function renderStationSelect() {
  const select = $("stationSelect");
  const previous = currentStationId;
  select.innerHTML = "";
  for (const station of streamsDoc.streams) {
    const opt = document.createElement("option");
    opt.value = station.id;
    opt.textContent = station.name + " · " + station.id;
    select.appendChild(opt);
  }
  currentStationId = streamsDoc.streams.some((s) => s.id === previous)
    ? previous
    : (streamsDoc.streams[0] ? streamsDoc.streams[0].id : null);
  select.value = currentStationId || "";
  updateStationHeader();
}

function updateStationHeader() {
  const station = selectedStation();
  const hasStation = Boolean(station);
  const total = streamsDoc.streams.length;
  $("stationSelect").disabled = !hasStation;
  $("btnDeleteStation").disabled = !hasStation || total <= 1;
  $("btnStart").disabled = !hasStation;
  $("btnRestart").disabled = !hasStation;
  $("btnStop").disabled = !hasStation;
  $("rpStationName").textContent = station ? station.name : "—";
  $("rpStationId").textContent = station ? "· " + station.id : "";
}

async function loadStreams(selectId) {
  const data = await (await fetch("/api/streams")).json();
  streamsDoc = data;
  stationMap = new Map(streamsDoc.streams.map((s) => [s.id, s]));
  if (selectId && stationMap.has(selectId)) currentStationId = selectId;
  renderStationSelect();
  const cfg = selectedDraftOrStation();
  if (cfg) fillForm(cfg);
  $("console").innerHTML = '<div class="empty">Лог пуст — нажмите «Старт»</div>';
  refreshCommand();
  await pollHealth();
  await pollLogs();
}

function selectStation(stationId) {
  if (stationId === currentStationId) return;
  rememberCurrentDraft();
  stopListen();
  currentStationId = stationId;
  $("stationSelect").value = stationId;
  $("console").innerHTML = '<div class="empty">Лог пуст — нажмите «Старт»</div>';
  const cfg = selectedDraftOrStation();
  if (cfg) fillForm(cfg);
  updateStationHeader();
  refreshCommand();
  pollHealth();
  pollLogs();
}

$("stationSelect").addEventListener("change", (e) => selectStation(e.target.value));

$("btnAddStation").addEventListener("click", async () => {
  const name = prompt("Имя новой станции:", "Радио " + (streamsDoc.streams.length + 1));
  if (name === null) return;
  try {
    const res = await fetch("/api/streams", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() || undefined }),
    });
    const data = await res.json();
    if (!data.ok) {
      toast(data.error || "Не удалось добавить станцию", true);
      return;
    }
    drafts.delete(data.station.id);
    await loadStreams(data.station.id);
    toast("Станция добавлена");
  } catch (e) {
    toast("Не удалось добавить станцию", true);
  }
});

$("btnDeleteStation").addEventListener("click", async () => {
  const station = selectedStation();
  if (!station) return;
  const running = healthById.get(station.id);
  const message = running && running.running
    ? "Остановить и удалить станцию «" + station.name + "»?"
    : "Удалить станцию «" + station.name + "»?";
  if (!confirm(message)) return;
  try {
    const res = await fetch("/api/streams/" + encodeURIComponent(station.id), { method: "DELETE" });
    const data = await res.json();
    if (!data.ok) {
      toast(data.error || "Не удалось удалить станцию", true);
      return;
    }
    if (currentStationId === station.id) {
      currentStationId = null;
      stopListen();
    }
    drafts.delete(station.id);
    await loadStreams();
    toast("Станция удалена");
  } catch (e) {
    toast("Не удалось удалить станцию", true);
  }
});

/* ---------- actions ---------- */

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  return res.json();
}

async function saveSelected() {
  if (!currentStationId) return false;
  const cfg = collectConfig();
  try {
    if ((streamsDoc.settings.ffmpeg_path || "") !== (cfg.ffmpeg_path || "")) {
      const settingsRes = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ffmpeg_path: cfg.ffmpeg_path || "" }),
      });
      const settingsData = await settingsRes.json();
      if (!settingsData.ok) {
        toast(settingsData.error || "Не удалось сохранить настройки", true);
        return false;
      }
      streamsDoc.settings.ffmpeg_path = settingsData.settings.ffmpeg_path || "";
    }

    const res = await fetch("/api/streams/" + encodeURIComponent(currentStationId), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    });
    const data = await res.json();
    if (!data.ok) {
      toast(data.error || "Не удалось сохранить станцию", true);
      return false;
    }
    stationMap.set(data.station.id, data.station);
    drafts.delete(data.station.id);
    renderStationSelect();
    return true;
  } catch (e) {
    toast("Не удалось сохранить станцию", true);
    return false;
  }
}

$("btnSave").addEventListener("click", async () => {
  const ok = await saveSelected();
  toast(ok ? "Настройки сохранены" : "Ошибка сохранения", !ok);
});

$("btnStart").addEventListener("click", async () => {
  if (!(await saveSelected())) return;
  const data = await postJSON("/api/streams/" + encodeURIComponent(currentStationId) + "/start");
  toast(data.message, !data.ok);
  pollHealth();
});

$("btnRestart").addEventListener("click", async () => {
  if (!(await saveSelected())) return;
  const data = await postJSON("/api/streams/" + encodeURIComponent(currentStationId) + "/restart");
  toast(data.message, !data.ok);
  pollHealth();
});

$("btnStop").addEventListener("click", async () => {
  const data = await postJSON("/api/streams/" + encodeURIComponent(currentStationId) + "/stop");
  toast(data.message, !data.ok);
  pollHealth();
});

$("btnCopy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("cmdBox").textContent);
    toast("Команда скопирована");
  } catch (e) {
    toast("Не удалось скопировать", true);
  }
});

$("btnClearLog").addEventListener("click", () => {
  $("console").innerHTML = "";
});

/* ---------- A/B listening ---------- */

const audio = new Audio();
audio.preload = "none";
let listening = null; // "input" | "output" | null
const LISTEN_HINT = "Мониторинг идёт отдельным MP3-каналом (задержка 1–3 с) — только для сравнения звука на слух";

function updateListenUI() {
  $("btnListenInput").classList.toggle("playing", listening === "input");
  $("btnListenOutput").classList.toggle("playing", listening === "output");
  $("listenStatus").textContent = listening
    ? "Играет: " + (listening === "input" ? "входной поток (исходный)" : "выходной поток (после обработки)")
    : LISTEN_HINT;
  $("listenStatus").style.color = listening ? "var(--green)" : "";
}

function stopListen() {
  listening = null;
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
  updateListenUI();
}

async function toggleListen(which) {
  if (listening === which) { stopListen(); return; }
  if (!(await saveSelected())) return;
  audio.pause();
  audio.volume = $("listen_vol").value / 100;
  audio.src = "/api/streams/" + encodeURIComponent(currentStationId) +
    "/listen/" + which + "?t=" + Date.now();
  listening = which;
  updateListenUI();
  try {
    await audio.play();
  } catch (e) {
    toast("Браузер заблокировал воспроизведение", true);
    stopListen();
  }
}

$("btnListenInput").addEventListener("click", () => toggleListen("input"));
$("btnListenOutput").addEventListener("click", () => toggleListen("output"));
$("btnListenStop").addEventListener("click", stopListen);
audio.addEventListener("error", () => {
  if (listening) { toast("Поток недоступен — проверьте источник/выход", true); stopListen(); }
});
audio.addEventListener("ended", stopListen);
$("listen_vol").addEventListener("input", () => {
  audio.volume = $("listen_vol").value / 100;
  $("listen_vol_val").textContent = $("listen_vol").value + "%";
});

/* ---------- status polling ---------- */

function fmtUptime(sec) {
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? h + "ч " : "") + (m ? m + "м " : "") + s + "с";
}

function updateSelectedStatus(status) {
  const station = selectedStation();
  $("rpStatus").classList.toggle("running", Boolean(status && status.running));
  $("rpStatusText").textContent = status && status.running ? "РАБОТАЕТ" : "ОСТАНОВЛЕН";
  $("rpPid").textContent = status && status.running ? status.pid : "—";
  $("rpUptime").textContent = status && status.running ? fmtUptime(status.uptime) : "—";
  $("rpRestarts").textContent = status ? status.restarts : 0;
  const isLiquidsoap = status && status.engine === "liquidsoap";
  $("rpFfmpeg").textContent = isLiquidsoap
    ? "Liquidsoap worker"
    : (status && status.ffmpeg_found
        ? (status.ffmpeg_version || "найден").split(" Copyright")[0].replace("ffmpeg version ", "v")
        : "НЕ НАЙДЕН");
  $("rpFfmpeg").style.color = status && (status.ffmpeg_found || isLiquidsoap) ? "" : "var(--red)";
  const running = Boolean(status && status.running);
  currentRunning = running;
  $("btnStart").disabled = !station || running;
  $("btnStop").disabled = !station || !running;
  syncGainLive();
}

/* Живая подстройка (telnet воркера) доступна только для работающей
   Liquidsoap-станции; для FFmpeg изменения применяются при перезапуске. */
function syncGainLive() {
  const engine = $("engine").value;
  const enabled = engine === "liquidsoap" && currentRunning;
  $("btnGainLive").disabled = !enabled;
  $("btnGainLive").title = enabled
    ? "Мгновенно применить к работающему воркеру (без перезапуска)"
    : "Доступно для работающей станции с движком Liquidsoap — иначе применится при перезапуске";
}

$("btnGainLive").addEventListener("click", async () => {
  if (!currentStationId) return;
  if (!$("gain_enabled").checked) {
    toast("Сначала включите усиление", true);
    return;
  }
  const db = parseFloat($("gain_db").value);
  try {
    const res = await fetch("/api/streams/" + encodeURIComponent(currentStationId) + "/adjust", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gain_db: db }),
    });
    const data = await res.json();
    toast(data.ok
      ? "Применено живьём: " + (db > 0 ? "+" : "") + db + " dB"
      : (data.error || "Не удалось подстроить"), !data.ok);
  } catch (e) {
    toast("Не удалось подстроить", true);
  }
});

/* Живой BYPASS: для работающей Liquidsoap-станции переключатель шлёт
   telnet-команду воркеру сразу; иначе значение применится при перезапуске. */
$("bypass").addEventListener("change", async () => {
  if (!currentStationId) return;
  const enabled = $("bypass").checked;
  if ($("engine").value !== "liquidsoap" || !currentRunning) return;
  try {
    const res = await fetch("/api/streams/" + encodeURIComponent(currentStationId) + "/bypass", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    const data = await res.json();
    toast(data.ok ? data.message : (data.error || "Не удалось переключить"), !data.ok);
    if (data.ok) {
      const st = streamsDoc.streams.find((s) => s.id === currentStationId);
      if (st) st.bypass = enabled;
      syncChain();
    } else {
      $("bypass").checked = !enabled; // откат при неудаче
      syncCardState();
    }
  } catch (e) {
    $("bypass").checked = !enabled;
    syncCardState();
    toast("Не удалось переключить", true);
  }
});

async function pollHealth() {
  try {
    lastHealth = await (await fetch("/api/health")).json();
    healthById = new Map(lastHealth.stations.map((s) => [s.id, s]));
    const counts = lastHealth.counts;
    $("rpSummary").textContent = counts.running + " работает · " + counts.stopped + " остановлено";

    if (currentStationId) {
      const st = await (await fetch("/api/streams/" + encodeURIComponent(currentStationId) + "/status")).json();
      updateSelectedStatus(st);
    }
  } catch (e) { /* ignore */ }
}
setInterval(pollHealth, 2000);

/* ---------- log polling ---------- */

async function pollLogs() {
  if (!currentStationId) return;
  try {
    const since = logCursors.get(currentStationId) || 0;
    const data = await (await fetch(
      "/api/streams/" + encodeURIComponent(currentStationId) + "/logs?since=" + since
    )).json();
    if (data.logs.length) {
      const box = $("console");
      const empty = box.querySelector(".empty");
      if (empty) empty.remove();
      for (const entry of data.logs) {
        const div = document.createElement("div");
        div.className = "line";
        if (entry.line.startsWith("[panel]")) div.classList.add("panel");
        if (/\berror\b|failed|invalid|не найден|не удалось/i.test(entry.line)) div.classList.add("err");
        const ts = document.createElement("span");
        ts.className = "ts";
        ts.textContent = entry.ts;
        div.appendChild(ts);
        div.appendChild(document.createTextNode(entry.line));
        box.appendChild(div);
        logCursors.set(currentStationId, entry.id);
      }
      while (box.children.length > 1500) box.removeChild(box.firstChild);
      if ($("autoscroll").checked) box.scrollTop = box.scrollHeight;
    }
  } catch (e) { /* ignore */ }
}
setInterval(pollLogs, 1000);

/* ---------- init ---------- */

for (const id of Object.keys(FIELDS)) {
  $(id).addEventListener("input", () => { syncLabels(); syncCardState(); refreshCommand(); });
}

buildEqBands();

// риски шкал под горизонтальными слайдерами (стилистика приборов анализатора)
document.querySelectorAll('.field input[type="range"], .listen-row input[type="range"]').forEach((el) => {
  const t = document.createElement("div");
  t.className = "ticks";
  el.insertAdjacentElement("afterend", t);
});

(async () => {
  try {
    await loadStreams();
  } catch (e) {
    toast("Не удалось загрузить реестр станций", true);
  }
  pollHealth();
  pollLogs();
})();
