"use strict";

const $ = (id) => document.getElementById(id);

/* ---------- tabs ---------- */

const TABS = ["processing", "output", "system"];

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
    if (currentStationId) {
      history.replaceState(null, "",
        "#/s/" + encodeURIComponent(currentStationId) + "/" + tab.dataset.tab);
    }
  });
});

/* ---------- router: дашборд потоков ↔ страница станции ---------- */

function currentView() {
  return document.body.dataset.view === "station" ? "station" : "dashboard";
}

function showDashboard() {
  stopListen();
  document.body.dataset.view = "dashboard";
  renderDashboard();
}

function showStation(stationId, tab) {
  document.body.dataset.view = "station";
  if (stationId && stationId !== currentStationId) selectStation(stationId);
  updateStationHeader();
  activateTab(TABS.includes(tab) ? tab : "processing");
  updateListenUI(); // подсветка кнопок прослушки — только для звучащей станции
}

function applyRoute() {
  const hash = location.hash || "#/";
  const m = hash.match(/^#\/s\/([^/]+)(?:\/(\w+))?/);
  if (m) {
    const id = decodeURIComponent(m[1]);
    if (stationMap.has(id)) {
      showStation(id, m[2]);
      return;
    }
    history.replaceState(null, "", "#/"); // станция удалена — на дашборд
    showDashboard();
    return;
  }
  const legacy = hash.match(/^#(processing|output|system)$/);
  if (legacy && streamsDoc.streams.length) {
    // старый формат адреса вкладки — конвертируем в маршрут станции
    const id = currentStationId || streamsDoc.streams[0].id;
    history.replaceState(null, "",
      "#/s/" + encodeURIComponent(id) + "/" + legacy[1]);
    showStation(id, legacy[1]);
    return;
  }
  showDashboard();
}

window.addEventListener("hashchange", applyRoute);

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

function currentEqGains() {
  return EQ_FREQS.map((_, i) => parseFloat($("eq_b" + i).value));
}

/* Живое применение EQ: для работающей Liquidsoap-станции отпускание слайдера,
   пресет, сброс и включение EQ шлют все 10 полос воркеру по telnet. */
async function applyEqLive() {
  if (!currentStationId) return false;
  if (!currentRunning) return false;
  if (!$("eq_enabled").checked) return false;
  const gains = currentEqGains();
  try {
    const res = await fetch("/api/streams/" + encodeURIComponent(currentStationId) + "/adjust", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ eq_gains: gains }),
    });
    const data = await res.json();
    if (!data.ok) {
      toast(data.error || "Не удалось применить EQ", true);
      return false;
    }
    const st = streamsDoc.streams.find((s) => s.id === currentStationId);
    if (st) st.equalizer = Object.assign({}, st.equalizer, { enabled: true, gains });
    toast("EQ применён живьём");
    return true;
  } catch (e) {
    toast("Не удалось применить EQ", true);
    return false;
  }
}

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
    slider.addEventListener("change", () => { applyEqLive(); });
    slider.addEventListener("dblclick", () => { slider.value = 0; syncEqLabels(); refreshCommand(); applyEqLive(); });
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
    applyEqLive();
  }
});

$("btnEqReset").addEventListener("click", () => {
  applyEqPreset("flat");
  $("eq_preset").value = "";
  applyEqLive();
});

$("eq_enabled").addEventListener("change", () => { applyEqLive(); });

/* ---------- config <-> form ---------- */

const FIELDS = {
  bypass:         ["bypass", "bool"],
  src_url:        ["source", "url"],
  src_reconnect:  ["source", "reconnect", "bool"],
  src_reconnect_delay_max: ["source", "reconnect_delay_max", "num"],
  fb_enabled:     ["fallback", "enabled", "bool"],
  fb_mode:        ["fallback", "mode"],
  fb_url:         ["fallback", "url"],
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
  syncFallbackState();
  syncGainLive();
}

/* Поля резерва: блок активен при включённом fb_enabled; URL — только в режиме url */
function syncFallbackState() {
  const on = $("fb_enabled").checked;
  $("fbFields").classList.toggle("disabled", !on);
  $("fbUrlField").style.display = $("fb_mode").value === "url" ? "" : "none";
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
  $("stName").textContent = station ? station.name : "—";
  $("stId").textContent = station ? "· " + station.id : "";
}

/* ---------- переименование потока (инлайн-поле в шапке) ---------- */

$("stName").addEventListener("click", () => {
  const station = selectedStation();
  if (!station || $("stNameEdit")) return;
  const span = $("stName");
  const input = document.createElement("input");
  input.id = "stNameEdit";
  input.value = station.name;
  input.maxLength = 80;
  input.spellcheck = false;
  span.replaceWith(input);
  input.focus();
  input.select();

  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const name = input.value.trim();
    input.replaceWith(span);
    if (!save || !name || name === station.name) {
      if (save && !name) toast("Имя не может быть пустым", true);
      updateStationHeader();
      return;
    }
    try {
      const res = await fetch("/api/streams/" + encodeURIComponent(station.id), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      const data = await res.json();
      if (!data.ok) {
        toast(data.error || "Не удалось переименовать", true);
        updateStationHeader();
        return;
      }
      stationMap.set(data.station.id, data.station);
      const idx = streamsDoc.streams.findIndex((s) => s.id === data.station.id);
      if (idx >= 0) streamsDoc.streams[idx] = data.station; // карточка дашборда читает streamsDoc
      drafts.delete(data.station.id);
      renderStationSelect();
      toast("Поток переименован");
    } catch (e) {
      toast("Не удалось переименовать", true);
      updateStationHeader();
    }
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    else if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
});

/* ---------- dashboard: карточки потоков ---------- */

function outputUrlOf(st) {
  const ice = st.icecast || {};
  return "icecast://" + ice.host + ":" + ice.port + "/" +
    String(ice.mount || "").replace(/^\/+/, "");
}

function chainStepsOf(cfg) {
  if (cfg.bypass) return null;
  const eq = cfg.equalizer || {}, gain = cfg.gain || {}, comp = cfg.compressor || {};
  const ln = cfg.loudnorm || {}, lim = cfg.limiter || {};
  return [
    { name: "EQ", on: eq.enabled && (eq.gains || []).some((g) => g !== 0) },
    { name: "GAIN", on: gain.enabled && parseFloat(gain.db) !== 0 },
    { name: "COMP", on: Boolean(comp.enabled) },
    { name: "LUFS", on: Boolean(ln.enabled) },
    { name: "LIM", on: Boolean(lim.enabled) },
  ];
}

function scardUrl(label, value) {
  const row = document.createElement("div");
  row.className = "scard-url";
  const k = document.createElement("span");
  k.className = "k";
  k.textContent = label;
  const v = document.createElement("span");
  v.className = "v";
  v.textContent = value || "—";
  v.title = value || "";
  row.append(k, v);
  return row;
}

function scardBtn(cls, label, act, id) {
  const b = document.createElement("button");
  b.className = cls;
  b.textContent = label;
  b.dataset.act = act;
  b.addEventListener("click", (e) => {
    e.stopPropagation(); // кнопка не должна проваливать в страницу станции
    dashAction(act, id);
  });
  return b;
}

/* Прослушка прямо с карточки дашборда: A — вход (исходный), B — выход
   (после обработки). Канал один на всю панель, как и на странице станции. */
function scardListenBtn(id, which) {
  const b = document.createElement("button");
  b.className = "btn scard-listen";
  b.dataset.station = id;
  b.dataset.which = which;
  b.textContent = which === "input" ? "🎧 A · вход" : "🎧 B · выход";
  b.title = which === "input"
    ? "Прослушка входа (исходный поток)"
    : "Прослушка выхода (после обработки)";
  b.addEventListener("click", (e) => {
    e.stopPropagation(); // кнопка не должна проваливать в страницу станции
    toggleCardListen(id, which);
  });
  return b;
}

async function dashAction(act, id) {
  if (act === "restart") stopListen();
  const data = await postJSON("/api/streams/" + encodeURIComponent(id) + "/" + act);
  toast(data.message || (data.ok ? "Готово" : "Ошибка"), !data.ok);
  pollHealth();
}

function renderDashboard() {
  const grid = $("dashGrid");
  grid.innerHTML = "";
  if (!streamsDoc.streams.length) {
    const empty = document.createElement("div");
    empty.className = "dash-empty";
    empty.textContent = "Потоков нет — нажмите «+ Поток», чтобы добавить первый";
    grid.appendChild(empty);
    return;
  }
  for (const st of streamsDoc.streams) {
    const health = healthById.get(st.id) || {};
    const card = document.createElement("div");
    card.className = "scard";
    card.dataset.id = st.id;
    card.tabIndex = 0;
    card.setAttribute("role", "button");
    card.title = "Открыть настройки потока";

    const head = document.createElement("div");
    head.className = "scard-head";
    const dot = document.createElement("span");
    dot.className = "scard-status";
    const name = document.createElement("span");
    name.className = "scard-name";
    name.textContent = st.name;
    const sid = document.createElement("span");
    sid.className = "scard-id";
    sid.textContent = st.id;
    head.append(dot, name, sid);

    const urls = document.createElement("div");
    urls.className = "scard-urls";
    urls.appendChild(scardUrl("ВХОД", (st.source && st.source.url) || "—"));
    urls.appendChild(scardUrl("ВЫХОД", outputUrlOf(st)));

    const chain = document.createElement("div");
    chain.className = "scard-chain";
    const steps = chainStepsOf(st);
    if (!steps) {
      const chip = document.createElement("span");
      chip.className = "chip bypass";
      chip.textContent = "BYPASS — ОБХОД";
      chain.appendChild(chip);
    } else {
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

    const meta = document.createElement("div");
    meta.className = "scard-meta";
    const mStatus = document.createElement("span");
    mStatus.className = "m-status";
    const mUptime = document.createElement("span");
    mUptime.className = "m-uptime";
    const mRestarts = document.createElement("span");
    mRestarts.className = "m-restarts";
    meta.append(mStatus, mUptime, mRestarts);
    if (st.bypass) {
      const b = document.createElement("span");
      b.className = "bypass";
      b.textContent = "BYPASS";
      meta.appendChild(b);
    }

    const actions = document.createElement("div");
    actions.className = "scard-actions";
    actions.appendChild(scardBtn("btn primary", "▶ Старт", "start", st.id));
    actions.appendChild(scardBtn("btn", "⟳ Перезапуск", "restart", st.id));
    actions.appendChild(scardBtn("btn danger", "■ Стоп", "stop", st.id));

    // прослушка A/B — отдельная нижняя строка карточки
    const listenRow = document.createElement("div");
    listenRow.className = "scard-listen-row";
    const listenLbl = document.createElement("span");
    listenLbl.className = "lbl";
    listenLbl.textContent = "Подслушка";
    listenRow.appendChild(listenLbl);
    listenRow.appendChild(scardListenBtn(st.id, "input"));
    listenRow.appendChild(scardListenBtn(st.id, "output"));

    card.append(head, urls, chain, meta, actions, listenRow);
    card.addEventListener("click", () => {
      location.hash = "#/s/" + encodeURIComponent(st.id) + "/processing";
    });
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); card.click(); }
    });
    grid.appendChild(card);
  }
  updateDashboardCards();
  updateListenUI(); // восстановить подсветку звучащей карточки после пересоздания DOM
}

/* Живое обновление карточек без пересоздания DOM (иначе поллинг каждые
   2 с съедал бы наведение и клики). Структуру перестраиваем, только если
   состав станций изменился. */
function updateDashboardCards() {
  if (currentView() !== "dashboard") return;
  const grid = $("dashGrid");
  const cards = Array.from(grid.querySelectorAll(".scard"));
  const ids = streamsDoc.streams.map((s) => s.id);
  if (cards.length !== ids.length || cards.some((c, i) => c.dataset.id !== ids[i])) {
    renderDashboard();
    return;
  }
  for (const card of cards) {
    const health = healthById.get(card.dataset.id) || {};
    const running = Boolean(health.running);
    /* Три состояния работающей карточки:
       красная — входной поток оборвался; жёлтая — вход есть, но выход
       не подключён к Icecast; зелёная — есть и вход, и выход.
       null (неизвестно, старый скрипт воркера) не считаем аварией. */
    let state = "";
    if (running) {
      if (health.input_connected === false) state = "no-input";
      else if (health.icecast_connected === false) state = "no-output";
      else state = "running";
    }
    card.classList.toggle("running", state === "running");
    card.classList.toggle("no-output", state === "no-output");
    card.classList.toggle("no-input", state === "no-input");
    card.querySelector(".scard-status").classList.toggle("running", state === "running");
    card.querySelector(".scard-status").classList.toggle("no-output", state === "no-output");
    card.querySelector(".scard-status").classList.toggle("no-input", state === "no-input");
    const statusText = !running ? "остановлено"
      : state === "no-input" ? "нет входа"
      : state === "no-output" ? "нет выхода"
      : "работает";
    card.querySelector(".m-status").textContent = statusText;
    card.querySelector(".m-uptime").textContent =
      running ? "аптайм " + fmtUptime(health.uptime || 0) : "";
    card.querySelector(".m-restarts").textContent = "рестартов " + (health.restarts || 0);
    card.querySelector('[data-act="start"]').disabled = running;
    card.querySelector('[data-act="stop"]').disabled = !running;
  }
}

async function loadStreams(selectId) {
  const data = await (await fetch("/api/streams")).json();
  streamsDoc = data;
  $("ffmpeg_path").value = (streamsDoc.settings && streamsDoc.settings.ffmpeg_path) || "";
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

$("btnBack").addEventListener("click", () => { location.hash = "#/"; });

// Строка «Выход» в Stream Info — клик открывает форму редактирования Icecast
$("rpOutputRow").addEventListener("click", () => {
  if (currentStationId) {
    location.hash = "#/s/" + encodeURIComponent(currentStationId) + "/output";
  }
});

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
    // новый поток — сразу проваливаемся в его настройки
    location.hash = "#/s/" + encodeURIComponent(data.station.id) + "/processing";
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
    // после удаления — назад на дашборд
    if (currentView() === "station") location.hash = "#/";
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
      $("ffmpeg_path").value = streamsDoc.settings.ffmpeg_path;
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
    const idx = streamsDoc.streams.findIndex((s) => s.id === data.station.id);
    if (idx >= 0) streamsDoc.streams[idx] = data.station; // карточки дашборда читают streamsDoc
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
  stopListen(); // поток воркера сейчас разорвётся — гасим мониторинг сами
  const data = await postJSON("/api/streams/" + encodeURIComponent(currentStationId) + "/restart");
  toast(data.message, !data.ok);
  pollHealth();
});

$("btnStop").addEventListener("click", async () => {
  stopListen();
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
let listenStationId = null; // какая станция звучит (дашборд и страница станции — один канал)
// Поколение прослушки: переключение/остановка инкрементируют его, чтобы
// асинхронные события старого потока (error/ended) не давали ложных тостов.
let listenGeneration = 0;
const LISTEN_HINT = "Мониторинг идёт отдельным MP3-каналом (задержка 1–3 с) — только для сравнения звука на слух";

function updateListenUI() {
  const own = listenStationId === currentStationId; // звучит ли выбранная станция
  $("btnListenInput").classList.toggle("playing", Boolean(listening) && own && listening === "input");
  $("btnListenOutput").classList.toggle("playing", Boolean(listening) && own && listening === "output");
  document.querySelectorAll(".scard-listen").forEach((b) => {
    b.classList.toggle("playing",
      Boolean(listening) && listenStationId === b.dataset.station && listening === b.dataset.which);
  });
  $("listenStatus").textContent = listening
    ? "Играет: " + (listening === "input" ? "входной поток (исходный)" : "выходной поток (после обработки)")
    : LISTEN_HINT;
  $("listenStatus").style.color = listening ? "var(--green)" : "";
}

/* Живой индикатор прослушки: раз в секунду смотрим, движется ли
   currentTime плеера. Движется — «звук идёт N с» (зелёный): байты дошли
   до браузера, если звука нет — ищите заглушенную вкладку/микшер.
   Стоит на месте ≥4 с — «поток не идёт» (красный): проблема в тракте
   панель→браузер или в источнике. */
let listenTimer = null;
let listenLastTime = -1;
let listenStalledSec = 0;

function stopListenWatchdog() {
  if (listenTimer) { clearInterval(listenTimer); listenTimer = null; }
}

function startListenWatchdog() {
  stopListenWatchdog();
  listenLastTime = -1;
  listenStalledSec = 0;
  listenTimer = setInterval(() => {
    if (!listening) { stopListenWatchdog(); return; }
    const t = audio.currentTime;
    if (t === listenLastTime) listenStalledSec++;
    else listenStalledSec = 0;
    listenLastTime = t;
    const base = listening === "input" ? "входной поток (исходный)" : "выходной поток (после обработки)";
    if (listenStalledSec >= 4) {
      $("listenStatus").textContent = "Играет: " + base + " · поток не идёт " + listenStalledSec + " с";
      $("listenStatus").style.color = "var(--red)";
    } else {
      $("listenStatus").textContent = "Играет: " + base + " · звук идёт " + Math.floor(t) + " с";
      $("listenStatus").style.color = "var(--green)";
    }
  }, 1000);
}

function stopListen() {
  listening = null;
  listenStationId = null;
  listenGeneration++;
  stopListenWatchdog();
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
  updateListenUI();
}

async function toggleListen(which) {
  if (listening === which && listenStationId === currentStationId) { stopListen(); return; }
  const gen = ++listenGeneration;
  audio.pause();
  audio.volume = $("listen_vol").value / 100;
  audio.src = "/api/streams/" + encodeURIComponent(currentStationId) +
    "/listen/" + which + "?t=" + Date.now();
  listening = which;
  listenStationId = currentStationId;
  updateListenUI();
  startListenWatchdog();
  // play() вызываем синхронно, в том же жесте клика: после любого await
  // браузер считает жест потраченным и блокирует автовоспроизведение.
  const playPromise = audio.play();
  const saved = await saveSelected();
  try {
    await playPromise;
  } catch (e) {
    // AbortError — play() отменён нашим же pause()/сменой src: быстрый
    // переключение A↔B или повторный клик прерывает неустановившееся
    // воспроизведение. Это не сбой потока — молча игнорируем.
    if (e && e.name === "AbortError") return;
    if (gen !== listenGeneration) return; // поток уже не актуален
    toast((saved
      ? "Браузер заблокировал воспроизведение"
      : "Настройки не сохранены — воспроизведение отменено") +
      " [" + (e && e.name) + ": " + (e && e.message) + "]", true);
    stopListen();
  }
}

/* Прослушка с карточки дашборда: тот же канал, что и на странице станции,
   но без привязки к выбранной станции и без сохранения формы. */
async function toggleCardListen(stationId, which) {
  if (listening === which && listenStationId === stationId) { stopListen(); return; }
  const gen = ++listenGeneration;
  audio.pause();
  audio.volume = $("listen_vol").value / 100;
  audio.src = "/api/streams/" + encodeURIComponent(stationId) +
    "/listen/" + which + "?t=" + Date.now();
  listening = which;
  listenStationId = stationId;
  updateListenUI();
  startListenWatchdog();
  try {
    // play() — синхронно в жесте клика, до любого await (см. toggleListen)
    await audio.play();
  } catch (e) {
    if (e && e.name === "AbortError") return; // своё же переключение A↔B
    if (gen !== listenGeneration) return;
    toast("Браузер заблокировал воспроизведение [" +
      (e && e.name) + ": " + (e && e.message) + "]", true);
    stopListen();
  }
}

$("btnListenInput").addEventListener("click", () => toggleListen("input"));
$("btnListenOutput").addEventListener("click", () => toggleListen("output"));
$("btnListenStop").addEventListener("click", stopListen);
audio.addEventListener("error", () => {
  // Событие от устаревшего/самого остановленного потока — не ошибка эфира.
  if (listening && listenGeneration > 0) {
    toast("Поток недоступен — проверьте источник/выход", true);
    stopListen();
  }
});
audio.addEventListener("ended", () => { listenGeneration++; stopListen(); });
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
  $("rpFfmpeg").textContent = "Liquidsoap worker";
  $("rpFfmpeg").style.color = "";
  const running = Boolean(status && status.running);
  currentRunning = running;
  $("btnStart").disabled = !station || running;
  $("btnStop").disabled = !station || !running;
  syncGainLive();
  syncIcecastStatus(status);
}

/* Статус подключения выхода к Icecast (карточка 07): живой флаг
   on_connect/on_disconnect воркера, опрашивается со статусом станции. */
function syncIcecastStatus(status) {
  const box = $("iceStatus");
  const text = $("iceStatusText");
  box.classList.remove("ok", "bad");
  if (!status || !status.running) {
    text.textContent = "Подключение к Icecast: воркер остановлен";
  } else if (status.icecast_connected === true) {
    box.classList.add("ok");
    text.textContent = "Подключено к Icecast";
  } else if (status.icecast_connected === false) {
    box.classList.add("bad");
    text.textContent = "Нет подключения к Icecast — воркер повторяет попытки";
  } else {
    text.textContent = "Статус подключения неизвестен — перезапустите воркер";
  }
}

/* Живая подстройка (telnet воркера) доступна для работающей станции;
   для остановленной изменения применяются при перезапуске. */
function syncGainLive() {
  const enabled = currentRunning;
  $("btnGainLive").disabled = !enabled;
  $("btnGainLive").title = enabled
    ? "Мгновенно применить к работающему воркеру (без перезапуска)"
    : "Доступно для работающей станции — иначе применится при перезапуске";
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

/* Автоприменение gain: для работающей Liquidsoap-станции движение ползунка
   само шлёт живую подстройку (с короткой задержкой после остановки). */
let gainLiveTimer = null;
$("gain_db").addEventListener("input", () => {
  if (!currentRunning) return;
  if (!$("gain_enabled").checked) return;
  clearTimeout(gainLiveTimer);
  gainLiveTimer = setTimeout(async () => {
    const db = parseFloat($("gain_db").value);
    try {
      const res = await fetch("/api/streams/" + encodeURIComponent(currentStationId) + "/adjust", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ gain_db: db }),
      });
      const data = await res.json();
      if (!data.ok) toast(data.error || "Не удалось подстроить gain", true);
    } catch (e) {
      toast("Не удалось подстроить gain", true);
    }
  }, 400);
});

/* Автоприменение динамики: для работающей Liquidsoap-станции движение
   ползунков компрессора/лимитера само шлёт живую подстройку (с задержкой
   после остановки, одним запросом на оба блока). Loudnorm не шлём — у него
   нет живого эквивалента, он применяется при перезапуске воркера. */
let dynLiveTimer = null;
function scheduleDynLive() {
  if (!currentStationId) return;
  if (!currentRunning) return;
  clearTimeout(dynLiveTimer);
  dynLiveTimer = setTimeout(async () => {
    const body = {};
    if ($("comp_enabled").checked) {
      body.compressor = {
        threshold: parseFloat($("comp_threshold").value),
        ratio: parseFloat($("comp_ratio").value),
        attack: parseFloat($("comp_attack").value),
        release: parseFloat($("comp_release").value),
        makeup: parseFloat($("comp_makeup").value),
      };
    }
    if ($("lim_enabled").checked) {
      body.limiter = {
        ceiling_db: parseFloat($("lim_ceiling_db").value),
        attack: parseFloat($("lim_attack").value),
        release: parseFloat($("lim_release").value),
      };
    }
    if (!body.compressor && !body.limiter) return;
    try {
      const res = await fetch("/api/streams/" + encodeURIComponent(currentStationId) + "/adjust", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!data.ok) toast(data.error || "Не удалось подстроить динамику", true);
    } catch (e) {
      toast("Не удалось подстроить динамику", true);
    }
  }, 400);
}

for (const id of ["comp_threshold", "comp_ratio", "comp_attack", "comp_release",
                  "comp_makeup", "lim_ceiling_db", "lim_attack", "lim_release"]) {
  $(id).addEventListener("input", scheduleDynLive);
}

/* Живое включение/выключение секций (как BYPASS): для работающей
   Liquidsoap-станции галочка секции сразу шлёт переключение воркеру;
   значение одновременно сохраняется в реестре. Секция, выключенная при
   старте воркера, включится после его перезапуска — бэкенд ответит
   понятным сообщением. */
const SECTION_BY_CHECKBOX = {
  eq_enabled: "equalizer",
  gain_enabled: "gain",
  comp_enabled: "compressor",
  ln_enabled: "loudnorm",
  lim_enabled: "limiter",
};
for (const [checkboxId, section] of Object.entries(SECTION_BY_CHECKBOX)) {
  $(checkboxId).addEventListener("change", async () => {
    if (!currentStationId) return;
    if (!currentRunning) return;
    const enabled = $(checkboxId).checked;
    try {
      const res = await fetch("/api/streams/" + encodeURIComponent(currentStationId) + "/adjust", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sections: { [section]: enabled } }),
      });
      const data = await res.json();
      toast(data.ok ? data.message : (data.error || "Не удалось переключить секцию"), !data.ok);
    } catch (e) {
      toast("Не удалось переключить секцию", true);
    }
  });
}

/* Живой BYPASS: для работающей Liquidsoap-станции переключатель шлёт
   telnet-команду воркеру сразу; иначе значение применится при перезапуске. */
$("bypass").addEventListener("change", async () => {
  if (!currentStationId) return;
  const enabled = $("bypass").checked;
  if (!currentRunning) return;
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
    const summary = counts.running + " работает · " + counts.stopped + " остановлено";
    $("rpSummary").textContent = summary;
    $("dashSummary").innerHTML = "<b>" + counts.running + "</b> работает · " + counts.stopped + " остановлено";
    updateDashboardCards();

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
  applyRoute();
  pollHealth();
  pollLogs();
})();
