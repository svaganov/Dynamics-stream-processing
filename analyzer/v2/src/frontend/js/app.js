/**
 * Stream Media Analyzer v2 — Frontend App
 * Sprint 1.5: Shared logic for audio/video analyzer templates
 */

// ============================================
// State
// ============================================

const state = {
  protocol: 'srt',
  analyzerType: 'audio',
  connected: false,
  metrics: {
    dbfs: { left: -70, right: -70, peak: -70, hold: -70 },
    lufs: { m: -70, s: -70, i: -70 },
    truePeak: { current: -70, max: -70 },
    spectrum: { bands: new Array(31).fill(-70), peakFreq: 0, peakDb: -70 },
    history: new Array(60).fill(-70),
    silence: { active: false, duration: 0 },
    lra: 0,
    network: { rtt: 0, bandwidth: 0, loss: 0, buffer: 0 },
    transport: { sync: true, ccErrors: 0, pcrJitter: 0, nullRatio: 0 },
    video: { codec: '', resolution: '', fps: 0, gop: '', keyframes: 0 },
    metadata: {},
  },
  throughputHistory: [],
  networkWindow: 300,
  lufsHistoryWindow: 300,
  animationId: null,
  ws: null,
  wsConnected: false,
  signalState: {
    baseLevel: -18,
    peakHoldL: -70,
    peakHoldR: -70,
    peakDecay: 0.15,
    lastPeakTimeL: 0,
    lastPeakTimeR: 0,
  },
};

// ============================================
// WebSocket Client
// ============================================

// [audio-panel] WebSocket — на тот же хост/порт, откуда загружена страница:
// панель публикует анализатор на случайном порту 127.0.0.1, а жёстко зашитый
// localhost:8765 вёл бы на чужой экземпляр анализатора (если он запущен).
const WS_URL = (window.location.protocol === 'https:' ? 'wss://' : 'ws://') +
  window.location.host + '/ws';

// Warn if opened via file://
if (window.location.protocol === 'file:') {
  console.error('[App] ERROR: Open via http://localhost:8765/app/ — file:// blocks WebSocket');
}

let _pendingMessages = [];

function connectWS() {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) return;
  if (state.ws) {
    try { state.ws.close(); } catch (e) {}
    state.ws = null;
  }

  const ws = new WebSocket(WS_URL);
  state.ws = ws;

  ws.onopen = () => {
    state.wsConnected = true;
    console.log('[WS] Connected');
    addAlert('success', 'WebSocket connected');
    // Subscribe to current protocol
    ws.send(JSON.stringify({ action: 'subscribe', protocol: state.protocol }));
    // Send any messages queued before connection opened
    _pendingMessages.forEach(msg => ws.send(JSON.stringify(msg)));
    _pendingMessages = [];
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      onWSMessage(msg);
    } catch (e) {
      console.error('[WS] Invalid JSON:', event.data);
    }
  };

  ws.onclose = () => {
    state.wsConnected = false;
    state.ws = null;
    console.log('[WS] Disconnected');
    addAlert('warning', 'WebSocket disconnected');
  };

  ws.onerror = (err) => {
    console.error('[WS] Error:', err);
    state.wsConnected = false;
    state.ws = null;
    addAlert('error', 'WebSocket error: ' + err.type);
  };
}

function disconnectWS() {
  if (state.ws) {
    state.ws.close();
    state.ws = null;
  }
  state.wsConnected = false;
  _pendingMessages = [];
}

function sendWS(data) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(data));
  } else {
    _pendingMessages.push(data);
    console.log('[WS] Queued message (connection not open yet):', data.action);
  }
}

function onWSMessage(msg) {
  switch (msg.type) {
    case 'connected':
      console.log('[WS] Server version:', msg.version);
      break;
    case 'subscribed':
      console.log('[WS] Subscribed to protocol:', msg.protocol);
      break;
    case 'metrics':
      updateMetricsFromWS(msg.data);
      break;
    case 'stream_status':
      updateStreamStatusFromWS(msg.status, msg.url);
      break;
    case 'metadata':
      console.log('[WS] Metadata received:', msg.data);
      updateMetadataFromWS(msg.data);
      break;
    case 'bitrate':
      updateThroughputFromWS(msg.kbps);
      break;
    case 'pong':
      break;
    case 'error':
      console.error('[WS] Server error:', msg.message);
      break;
    default:
      console.log('[WS] Unknown message:', msg);
  }
}

function updateMetricsFromWS(data) {
  const m = state.metrics;

  if (data.dbfs) {
    m.dbfs.left = data.dbfs.left ?? m.dbfs.left;
    m.dbfs.right = data.dbfs.right ?? m.dbfs.right;
    m.dbfs.peak = data.dbfs.peak ?? m.dbfs.peak;
    m.dbfs.hold = data.dbfs.peak_hold ?? m.dbfs.hold;
  }

  if (data.lufs) {
    m.lufs.m = data.lufs.momentary ?? m.lufs.m;
    m.lufs.s = data.lufs.short_term ?? m.lufs.s;
    m.lufs.i = data.lufs.integrated ?? m.lufs.i;
  }

  if (data.true_peak) {
    m.truePeak.current = data.true_peak.current ?? m.truePeak.current;
    m.truePeak.max = data.true_peak.max ?? m.truePeak.max;
  }

  if (data.spectrum && data.spectrum.bands) {
    m.spectrum.bands = data.spectrum.bands;
    m.spectrum.peakFreq = data.spectrum.peak_freq_hz ?? m.spectrum.peakFreq;
    m.spectrum.peakDb = data.spectrum.peak_db ?? m.spectrum.peakDb;
  }

  if (data.loudness_history && data.loudness_history.values) {
    m.history = data.loudness_history.values;
  }

  if (data.silence) {
    m.silence.active = data.silence.active;
    m.silence.duration = data.silence.duration_sec;
  }

  if (data.lra !== undefined) {
    m.lra = data.lra;
  }
}

function updateStreamStatusFromWS(status, url) {
  const text = document.getElementById('status-text');
  if (text) {
    if (status === 'connecting') text.textContent = 'Connecting...';
    else if (status === 'connected') text.textContent = 'Connected';
    else if (status === 'disconnected') text.textContent = 'Disconnected';
    else if (status === 'error') text.textContent = 'Error';
  }

  const dot = document.getElementById('status-dot');
  if (dot) {
    dot.classList.toggle('disconnected', status === 'disconnected');
  }
}

function updateThroughputFromWS(kbps) {
  state.throughputHistory.push(kbps);
  // Keep up to 60 minutes of samples (sample every ~2 seconds → 1800 points)
  const MAX_THROUGHPUT_POINTS = 1800;
  while (state.throughputHistory.length > MAX_THROUGHPUT_POINTS) {
    state.throughputHistory.shift();
  }
  // Update network panel bandwidth display (convert kbps → Mbps)
  state.network.bandwidth = kbps / 1000;
  const el = document.getElementById('net-bandwidth');
  if (el) el.textContent = state.network.bandwidth.toFixed(2) + ' Mbps';
}

function updateMetadataFromWS(data) {
  try {
    state.metrics.metadata = { ...state.metrics.metadata, ...data };

    // Stream Metadata (meta-* ids)
    const metaMap = {
      'meta-title': data.title || data.stream_title,
      'meta-artist': data.artist,
      'meta-album': data.album,
      'meta-year': data.year || data.date,
      'meta-genre': data.genre,
      'meta-comment': data.comment,
      'meta-station': data.station,
      'meta-icy': data.station,
    };

    Object.entries(metaMap).forEach(([id, value]) => {
      if (value != null) {
        const el = document.getElementById(id);
        if (el) {
          el.textContent = String(value);
        } else {
          console.warn('[Metadata] Element not found:', id);
        }
      }
    });

    // Technical Data (tech-* ids)
    const techMap = {
      'tech-codec': data.codec,
      'tech-profile': data.profile,
      'tech-channels': data.channels,
      'tech-bitrate': data.bitrate,
      'tech-samplerate': data.samplerate,
      'tech-mode': data.mode,
    };

    Object.entries(techMap).forEach(([id, value]) => {
      if (value != null) {
        const el = document.getElementById(id);
        if (el) el.textContent = String(value);
      }
    });

    // Update detected format badge from backend metadata (container or codec)
    if (data.container || data.codec) {
      const format = data.container || data.codec;
      updateProtocolBadge(format);
    }
  } catch (e) {
    console.error('[Metadata] Update failed:', e);
  }
}

// ============================================
// Initialization
// ============================================

function initAnalyzer(options = {}) {
  state.analyzerType = options.type || 'audio';
  state.protocol = getProtocolFromURL();

  updateProtocolBadge();
  updateStreamInfo();
  updateConnectionStatus();
  setupTabs();
  setupTimeButtons();
  setupConnectButtons();
  setupAudioPreview();
  setupTransportVisibility();

  // Auto-connect only for video analyzer (demo mode)
  // Audio analyzer requires manual Connect to start WebSocket + pipeline
  if (state.analyzerType === 'video') {
    connect();
  }

  // [audio-panel] Автоподключение по ?url=<stream>: панель управления
  // открывает анализатор сразу с адресом потока станции.
  const panelAutoUrl = new URLSearchParams(window.location.search).get('url');
  if (panelAutoUrl && state.analyzerType === 'audio') {
    const urlInput = document.getElementById('connect-url');
    if (urlInput) urlInput.value = panelAutoUrl;
    setAudioPreviewUrl(panelAutoUrl);
    connect();
    sendWS({ action: 'connect_stream', url: panelAutoUrl });
  }
}

function getProtocolFromURL() {
  const params = new URLSearchParams(window.location.search);
  return params.get('protocol') || (state.analyzerType === 'video' ? 'srt' : 'auto');
}

function updateProtocolBadge(detectedFormat) {
  const badge = document.getElementById('protocol-badge');
  if (!badge) return;

  if (detectedFormat) {
    badge.textContent = detectedFormat.toUpperCase();
  } else if (state.protocol && state.protocol !== 'unknown') {
    badge.textContent = state.protocol.toUpperCase();
  } else {
    badge.textContent = 'AUTO';
  }
}

function updateStreamInfo() {
  const defaults = {
    mp3: { bitrate: '128 kbps', codec: 'MP3', samplerate: '44.1 kHz' },
    aac: { bitrate: '256 kbps', codec: 'AAC', samplerate: '48 kHz' },
    srt: { bitrate: '8.5 Mbps', codec: 'H.264', resolution: '1920x1080', fps: '50' },
    rtmp: { bitrate: '6.2 Mbps', codec: 'H.264', resolution: '1280x720', fps: '30' },
    'mpeg-ts': { bitrate: '12 Mbps', codec: 'H.264', resolution: '1920x1080', fps: '25' },
    hls: { bitrate: '4.8 Mbps', codec: 'H.264', resolution: '1280x720', fps: '25' },
  };

  const d = defaults[state.protocol] || {};

  const fields = {
    'info-bitrate': d.bitrate || '—',
    'info-samplerate': d.samplerate || '48 kHz',
    'info-codec': d.codec || '—',
    'info-resolution': d.resolution || '—',
    'info-fps': d.fps || '—',
    'info-vbitrate': d.bitrate || '—',
    'info-abitrate': '192 kbps',
  };

  Object.entries(fields).forEach(([id, value]) => {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  });
}

function setupTabs() {
  document.querySelectorAll('.nav-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const tabName = tab.dataset.tab;
      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      const target = document.getElementById('tab-' + tabName);
      if (target) target.classList.add('active');
    });
  });
}

function setupTimeButtons() {
  document.querySelectorAll('.time-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const parent = btn.closest('.time-window-selector');
      if (parent) {
        parent.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');

        // Loudness history window selector
        if (parent.id === 'history-time-selector') {
          const windowSec = parseInt(btn.dataset.window, 10);
          if (!isNaN(windowSec)) {
            state.lufsHistoryWindow = windowSec;
          }
        }

        // Network throughput window selector
        if (parent.id === 'network-time-selector') {
          const windowSec = parseInt(btn.dataset.window, 10);
          if (!isNaN(windowSec)) {
            state.networkWindow = windowSec;
          }
        }
      }
    });
  });
}

function setupConnectButtons() {
  const btnConnect = document.getElementById('btn-connect');
  const btnDisconnect = document.getElementById('btn-disconnect');

  if (btnConnect) {
    btnConnect.addEventListener('click', () => {
      const url = document.getElementById('connect-url')?.value;
      updateProtocolBadge('AUTO');
      setAudioPreviewUrl(url);
      connect();
      sendWS({ action: 'connect_stream', url });
      addAlert('info', 'Connecting to ' + url);
    });
  }

  if (btnDisconnect) {
    btnDisconnect.addEventListener('click', () => {
      setAudioPreviewUrl(null);
      disconnect();
      updateProtocolBadge('AUTO');
      sendWS({ action: 'disconnect_stream' });
      addAlert('warning', 'Disconnected');
    });
  }
}

// ============================================
// Audio Preview
// ============================================

function setupAudioPreview() {
  const btn = document.getElementById('preview-play-btn');
  if (!btn) return;

  btn.addEventListener('click', () => {
    toggleAudioPreview();
  });
}

function setAudioPreviewUrl(url) {
  const audio = document.getElementById('preview-audio');
  const btn = document.getElementById('preview-play-btn');
  if (!audio || !btn) return;

  stopAudioPreview();

  if (url) {
    audio.src = url;
    audio.load();
    btn.disabled = false;
  } else {
    audio.removeAttribute('src');
    btn.disabled = true;
  }
}

function toggleAudioPreview() {
  const audio = document.getElementById('preview-audio');
  const btn = document.getElementById('preview-play-btn');
  if (!audio || !btn) return;

  if (audio.paused) {
    audio.play()
      .then(() => {
        updatePreviewIcon(true);
        addAlert('success', 'Audio preview started');
      })
      .catch((e) => {
        console.error('[Preview] Play failed:', e);
        addAlert('error', 'Could not start audio preview');
      });
  } else {
    audio.pause();
    updatePreviewIcon(false);
  }
}

function stopAudioPreview() {
  const audio = document.getElementById('preview-audio');
  if (audio) {
    audio.pause();
    audio.currentTime = 0;
  }
  updatePreviewIcon(false);
}

function updatePreviewIcon(playing) {
  const btn = document.getElementById('preview-play-btn');
  const icon = document.getElementById('preview-icon');
  const label = document.getElementById('preview-label');
  if (!btn || !icon || !label) return;

  btn.classList.toggle('is-playing', playing);
  label.textContent = playing ? 'Стоп' : 'Слушать';

  if (playing) {
    // Muted / stop icon (speaker with cross)
    icon.innerHTML = `<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"></polygon><line x1="23" y1="9" x2="17" y2="15"></line><line x1="17" y1="9" x2="23" y2="15"></line></svg>`;
  } else {
    // Speaker icon
    icon.innerHTML = `<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"></polygon><path d="M15.54 8.46a5 5 0 0 1 0 7.07"></path><path d="M19.07 4.93a10 10 0 0 1 0 14.14"></path></svg>`;
  }
}

function setupTransportVisibility() {
  const transportCard = document.getElementById('transport-card');
  if (transportCard) {
    transportCard.style.display = state.protocol === 'mpeg-ts' ? 'block' : 'none';
  }
}

// ============================================
// Connection / Simulation
// ============================================

function connect() {
  console.log('[Connect] Starting connection...');
  state.connected = true;
  state.throughputHistory = [];
  updateConnectionStatus();
  connectWS();
  startSimulation();
}

function useDemoSimulation() {
  return state.analyzerType === 'video' && !state.wsConnected;
}

function disconnect() {
  state.connected = false;
  updateConnectionStatus();
  disconnectWS();
  stopSimulation();
}

function updateConnectionStatus() {
  const dot = document.getElementById('status-dot');
  const text = document.getElementById('status-text');
  const form = document.getElementById('connect-form');
  if (dot) dot.classList.toggle('disconnected', !state.connected);
  if (text) text.textContent = state.connected ? 'Connected' : 'Disconnected';
  if (form) form.classList.toggle('connected', state.connected);
}

// ============================================
// Demo Simulation
// ============================================

function startSimulation() {
  let time = 0;

  function tick() {
    try {
      time += 0.02;
      if (useDemoSimulation()) {
        simulateMetrics(time);
      }
      updateUI();
      drawSpectrum();
      drawHistory();
      updateSrtCharts();
    } catch (e) {
      console.error('[Tick] Error:', e);
    }
    state.animationId = requestAnimationFrame(tick);
  }

  tick();
}

function stopSimulation() {
  if (state.animationId) {
    cancelAnimationFrame(state.animationId);
    state.animationId = null;
  }
}

function simulateMetrics(t) {
  const s = state.metrics;

  // Simulate -20 dBFS sine wave with variation
  const baseLevel = -20 + Math.sin(t * 2) * 3;
  const left = baseLevel + Math.random() * 1 - 0.5;
  const right = baseLevel + Math.random() * 1 - 0.5;
  const peak = Math.max(left, right);

  s.dbfs.left = Math.max(-70, Math.min(0, left));
  s.dbfs.right = Math.max(-70, Math.min(0, right));
  s.dbfs.peak = Math.max(-70, Math.min(0, peak));

  // LUFS (slightly lower than dBFS)
  s.lufs.m = s.dbfs.peak - 3 + Math.sin(t) * 1;
  s.lufs.s = s.lufs.m + Math.sin(t * 0.5) * 0.5;
  s.lufs.i = s.lufs.s + Math.sin(t * 0.1) * 0.3;

  // True Peak
  s.truePeak.current = s.dbfs.peak + 1.2;
  s.truePeak.max = Math.max(s.truePeak.max, s.truePeak.current);

  // Spectrum
  for (let i = 0; i < 31; i++) {
    const freq = 20 * Math.pow(2, i / 31 * Math.log2(20000 / 20));
    const dist = Math.abs(freq - 1000) / 1000;
    s.spectrum.bands[i] = baseLevel - dist * 15 + Math.random() * 3;
  }
  s.spectrum.peakFreq = 1000 + Math.sin(t) * 50;
  s.spectrum.peakDb = baseLevel;

  // History
  if (Math.random() > 0.98) {
    s.history.push(s.lufs.s);
    s.history.shift();
  }

  // Silence
  s.silence.active = baseLevel < -58;
  s.silence.duration = s.silence.active ? s.silence.duration + 0.02 : 0;

  // LRA
  s.lra = 8 + Math.sin(t * 0.3) * 2;

  // Network (video only)
  if (state.analyzerType === 'video') {
    s.network.rtt = 20 + Math.sin(t) * 5;
    s.network.bandwidth = 12 + Math.sin(t * 0.7) * 2;
    s.network.loss = Math.max(0, Math.sin(t * 3) * 0.1);
    s.network.buffer = 100 + Math.sin(t * 0.5) * 20;
  }

  // Transport
  if (state.protocol === 'mpeg-ts') {
    s.transport.ccErrors = Math.floor(Math.random() * 0.01);
    s.transport.pcrJitter = 10 + Math.sin(t) * 5;
    s.transport.nullRatio = 2 + Math.sin(t * 0.2) * 1;
  }
}

// ============================================
// UI Updates
// ============================================

function updateUI() {
  const s = state.metrics;

  // Legacy audio-analyzer elements
  setHeight('dbfs-left', dbfsToPercent(s.dbfs.left));
  setHeight('dbfs-right', dbfsToPercent(s.dbfs.right));
  setColor('dbfs-left', dbfsToColor(s.dbfs.left));
  setColor('dbfs-right', dbfsToColor(s.dbfs.right));
  setText('dbfs-peak-value', s.dbfs.peak.toFixed(1) + ' dBFS');
  setText('dbfs-hold-value', s.dbfs.hold.toFixed(1) + ' dBFS');

  setWidth('lufs-m-bar', lufsToPercent(s.lufs.m));
  setWidth('lufs-s-bar', lufsToPercent(s.lufs.s));
  setWidth('lufs-i-bar', lufsToPercent(s.lufs.i));
  setColor('lufs-m-bar', lufsToColor(s.lufs.m));
  setColor('lufs-s-bar', lufsToColor(s.lufs.s));
  setColor('lufs-i-bar', lufsToColor(s.lufs.i));
  setText('lufs-m', s.lufs.m.toFixed(1));
  setText('lufs-s', s.lufs.s.toFixed(1));
  setText('lufs-i', s.lufs.i.toFixed(1));

  setWidth('true-peak-bar', dbfsToPercent(s.truePeak.current));
  setWidth('true-peak-max-bar', dbfsToPercent(s.truePeak.max));
  setText('true-peak', s.truePeak.current.toFixed(1));
  setText('true-peak-max', s.truePeak.max.toFixed(1));

  setText('silence-status', s.silence.active ? 'ACTIVE' : 'Inactive');
  setText('silence-duration', s.silence.duration.toFixed(1) + ' s');

  setWidth('lra-bar', Math.min(100, s.lra * 5));
  setText('lra-value', s.lra.toFixed(1) + ' LU');

  setText('net-rtt', s.network.rtt.toFixed(1) + ' ms');
  setText('net-bandwidth', s.network.bandwidth.toFixed(1) + ' Mbps');
  setText('net-loss', s.network.loss.toFixed(2) + '%');
  setText('net-buffer', s.network.buffer.toFixed(0) + ' ms');

  setText('ts-sync', s.transport.sync ? 'Locked' : 'Lost');
  setText('ts-cc', s.transport.ccErrors.toString());
  setText('ts-pcr', s.transport.pcrJitter.toFixed(0) + ' ns');
  setText('ts-null', s.transport.nullRatio.toFixed(1) + '%');

  // Duration counter
  const dur = document.getElementById('info-duration');
  if (dur && state.connected) {
    const sec = Math.floor(performance.now() / 1000);
    const m = Math.floor(sec / 60).toString().padStart(2, '0');
    const s_ = (sec % 60).toString().padStart(2, '0');
    dur.textContent = `${m}:${s_}`;
  }

  // Segmented DBFS meters (video analyzer)
  updateSegmentedDbfs();

  // LUFS 3-scale meters (video analyzer)
  updateLufsMeters();
}

// ============================================
// Segmented DBFS Meter Animation
// ============================================

function updateSegmentedDbfs() {
  const s = state.metrics;
  const sig = state.signalState;
  const t = Date.now() / 1000;

  // Generate realistic audio signal
  const dbfsL = generateAudioSignal(t, 'L');
  const dbfsR = generateAudioSignal(t, 'R');

  // Update peak holds
  const phL = updatePeakHold(dbfsL, sig.peakHoldL, sig.lastPeakTimeL, t);
  sig.peakHoldL = phL.val;
  sig.lastPeakTimeL = phL.time;

  const phR = updatePeakHold(dbfsR, sig.peakHoldR, sig.lastPeakTimeR, t);
  sig.peakHoldR = phR.val;
  sig.lastPeakTimeR = phR.time;

  // Bar height in px (max 252px)
  const maxPx = 252;
  const pxL = maxPx - (Math.abs(dbfsL) / 70) * maxPx;
  const pxR = maxPx - (Math.abs(dbfsR) / 70) * maxPx;
  console.log('[DBFS] L=' + dbfsL.toFixed(1) + ' px=' + pxL.toFixed(0) + ' | R=' + dbfsR.toFixed(1) + ' px=' + pxR.toFixed(0));

  const containerL = document.getElementById('dbfsBarContainerL');
  const containerR = document.getElementById('dbfsBarContainerR');
  if (containerL) containerL.style.height = pxL + 'px';
  if (containerR) containerR.style.height = pxR + 'px';

  // Color
  function getColorHex(dbfs) {
    if (dbfs >= -2) return '#f87171';
    if (dbfs >= -9) return '#fb923c';
    if (dbfs >= -18) return '#fbbf24';
    return '#4ade80';
  }

  const cL = getColorHex(dbfsL);
  const cR = getColorHex(dbfsR);

  const valL = document.getElementById('dbfsValL');
  const valR = document.getElementById('dbfsValR');
  if (valL) {
    valL.textContent = dbfsL.toFixed(1);
    valL.style.color = cL;
    const labels = valL.parentNode?.querySelectorAll('.dbfs-readout__value');
    if (labels && labels[1]) labels[1].style.color = cL;
  }
  if (valR) {
    valR.textContent = dbfsR.toFixed(1);
    valR.style.color = cR;
    const labels = valR.parentNode?.querySelectorAll('.dbfs-readout__value');
    if (labels && labels[1]) labels[1].style.color = cR;
  }

  // Peak hold lines
  const peakL = document.getElementById('dbfsPeakL');
  const peakR = document.getElementById('dbfsPeakR');
  const holdPxL = maxPx - (Math.abs(sig.peakHoldL) / 70) * maxPx;
  const holdPxR = maxPx - (Math.abs(sig.peakHoldR) / 70) * maxPx;
  if (peakL) peakL.style.bottom = (4 + holdPxL) + 'px';
  if (peakR) peakR.style.bottom = (4 + holdPxR) + 'px';

  const holdL = document.getElementById('peakHoldL');
  const holdR = document.getElementById('peakHoldR');
  if (holdL) {
    holdL.textContent = sig.peakHoldL.toFixed(1);
    holdL.style.color = getColorHex(sig.peakHoldL);
    const labels = holdL.parentNode?.querySelectorAll('.dbfs-readout__value');
    if (labels && labels[1]) labels[1].style.color = getColorHex(sig.peakHoldL);
  }
  if (holdR) {
    holdR.textContent = sig.peakHoldR.toFixed(1);
    holdR.style.color = getColorHex(sig.peakHoldR);
    const labels = holdR.parentNode?.querySelectorAll('.dbfs-readout__value');
    if (labels && labels[1]) labels[1].style.color = getColorHex(sig.peakHoldR);
  }
}

function generateAudioSignal(t, channel) {
  // Audio analyzer: no demo signal — silence until real data arrives
  if (state.analyzerType === 'audio' && !state.wsConnected) {
    return -70;
  }

  // Use real WebSocket data if available
  if (state.wsConnected) {
    return channel === 'L' ? state.metrics.dbfs.left : state.metrics.dbfs.right;
  }

  // Demo simulation fallback (video only)
  const phase = channel === 'L' ? 0 : 0.7;
  const slow = Math.sin(t * 0.5 + phase) * 8;
  const fast = Math.sin(t * 3 + phase * 2) * 4;
  const noise = (Math.random() - 0.5) * 3;
  let dbfs = -18 + slow + fast + noise;
  if (Math.sin(t * 2.1 + phase) > 0.92) {
    dbfs += 8 + Math.random() * 6;
  }
  return Math.max(-70, Math.min(0, dbfs));
}

function updatePeakHold(current, hold, lastTime, t) {
  if (current > hold) {
    return { val: current, time: t };
  }
  const elapsed = t - lastTime;
  const decay = state.signalState.peakDecay * elapsed * 60;
  return { val: Math.max(current, hold - decay), time: lastTime };
}

// ============================================
// LUFS 3-Scale Meter Animation
// ============================================

function updateLufsMeters() {
  const time = Date.now() / 1000;

  let m, s, i, tp, lra;

  if (state.wsConnected) {
    m = state.metrics.lufs.m;
    s = state.metrics.lufs.s;
    i = state.metrics.lufs.i;
    tp = state.metrics.truePeak.current;
    lra = state.metrics.lra;
  } else if (state.analyzerType === 'audio') {
    // No demo signal for audio analyzer — silence until real data arrives
    m = -70;
    s = -70;
    i = -70;
    tp = -70;
    lra = 0;
  } else {
    const baseSignal = -24 + Math.sin(time * 0.8) * 4;
    const modulation = Math.sin(time * 2.5) * 3 + Math.sin(time * 5.2) * 1.5;
    const peaks = Math.max(0, Math.sin(time * 8) * 5 - 3) * Math.random();
    const noise = (Math.random() - 0.5) * 1.5;

    m = baseSignal + modulation + peaks + noise;
    s = m * 0.6 + (-24) * 0.4 + Math.sin(time * 1.2) * 2;
    i = s * 0.4 + (-25) * 0.6 + Math.sin(time * 0.5) * 1;
    tp = Math.max(m, -1.5) + 2.5 + Math.random() * 0.5;
    lra = 4 + Math.sin(time * 0.3) * 2 + Math.random() * 0.5;
  }

  const clampM = Math.max(-40, Math.min(0, m));
  const clampS = Math.max(-40, Math.min(0, s));
  const clampI = Math.max(-40, Math.min(0, i));
  const clampTP = Math.max(-6, Math.min(0, tp));

  // Bar heights (0..-40 scale, max 252px)
  const maxPx = 252;
  const pxM = maxPx - (Math.abs(clampM) / 40) * maxPx;
  const pxS = maxPx - (Math.abs(clampS) / 40) * maxPx;
  const pxI = maxPx - (Math.abs(clampI) / 40) * maxPx;

  const containerM = document.getElementById('lufsBarContainerM');
  const containerS = document.getElementById('lufsBarContainerS');
  const containerI = document.getElementById('lufsBarContainerI');
  if (containerM) containerM.style.height = pxM + 'px';
  if (containerS) containerS.style.height = pxS + 'px';
  if (containerI) containerI.style.height = pxI + 'px';

  // Color by zone
  function getColorClass(val) {
    if (val > -23.5) return 'lufs-readout__value--danger';
    if (val > -26) return 'lufs-readout__value--warning';
    return 'lufs-readout__value--safe';
  }
  function getColorHex(val) {
    if (val > -23.5) return '#f87171';
    if (val > -26) return '#fbbf24';
    return '#4ade80';
  }

  // Update M readout + TP
  const valM = document.getElementById('lufsValM');
  const tpValM = document.getElementById('tpValM');
  if (valM) {
    valM.textContent = clampM.toFixed(1);
    valM.className = 'lufs-readout__value ' + getColorClass(clampM);
    valM.style.color = getColorHex(clampM);
    const labels = valM.parentNode?.querySelectorAll('.lufs-readout__value');
    if (labels && labels[1]) labels[1].style.color = getColorHex(clampM);
  }
  if (tpValM) {
    tpValM.textContent = clampTP.toFixed(1);
    tpValM.className = 'lufs-readout__value ' + (clampTP > -1 ? 'lufs-readout__value--danger' : 'lufs-readout__value--safe');
    tpValM.style.color = clampTP > -1 ? '#f87171' : '#4ade80';
    const labels = tpValM.parentNode?.querySelectorAll('.lufs-readout__value');
    if (labels && labels[1]) labels[1].style.color = clampTP > -1 ? '#f87171' : '#4ade80';
  }

  // Update S readout
  const valS = document.getElementById('lufsValS');
  if (valS) {
    valS.textContent = clampS.toFixed(1);
    valS.className = 'lufs-readout__value ' + getColorClass(clampS);
    valS.style.color = getColorHex(clampS);
    const labels = valS.parentNode?.querySelectorAll('.lufs-readout__value');
    if (labels && labels[1]) labels[1].style.color = getColorHex(clampS);
  }

  // Update I readout
  const valI = document.getElementById('lufsValI');
  if (valI) {
    valI.textContent = clampI.toFixed(1);
    valI.className = 'lufs-readout__value ' + getColorClass(clampI);
    valI.style.color = getColorHex(clampI);
    const labels = valI.parentNode?.querySelectorAll('.lufs-readout__value');
    if (labels && labels[1]) labels[1].style.color = getColorHex(clampI);
  }

  // Update LRA
  const lraVal = document.getElementById('lraVal');
  if (lraVal) lraVal.textContent = lra.toFixed(1);

  // Peak hold lines
  const peakM = document.getElementById('lufsPeakM');
  const peakS = document.getElementById('lufsPeakS');
  const peakI = document.getElementById('lufsPeakI');
  const holdM = Math.max(clampM, -20 + Math.sin(time * 0.5) * 3);
  const holdS = Math.max(clampS, -22 + Math.sin(time * 0.3) * 2);
  const holdI = Math.max(clampI, -24 + Math.sin(time * 0.2) * 1);
  if (peakM) peakM.style.bottom = (4 + maxPx - (Math.abs(holdM) / 40) * maxPx) + 'px';
  if (peakS) peakS.style.bottom = (4 + maxPx - (Math.abs(holdS) / 40) * maxPx) + 'px';
  if (peakI) peakI.style.bottom = (4 + maxPx - (Math.abs(holdI) / 40) * maxPx) + 'px';

  // TP line on M scale
  const tpLineM = document.getElementById('lufsTpLineM');
  if (tpLineM) {
    const tpPx = maxPx - (Math.abs(clampTP) / 6) * maxPx;
    tpLineM.style.bottom = (4 + tpPx) + 'px';
  }
}

// ============================================
// SRT Charts
// ============================================

function updateSrtCharts() {
  const time = Date.now() / 1000;

  if (state.analyzerType === 'audio' && state.wsConnected) {
    // Real network throughput for audio analyzer
    const history = state.throughputHistory;
    const windowSec = state.networkWindow || 300;
    // Bitrate samples arrive roughly every 2 seconds
    const samplesPerSec = 0.5;
    const windowPoints = Math.min(history.length, Math.ceil(windowSec * samplesPerSec));
    const visibleHistory = history.slice(-windowPoints).reverse();
    const maxVal = Math.max(...visibleHistory, 500);
    drawNetworkThroughputChart();
    // Update chart title
    const chartCard = document.querySelector('#bwChart')?.closest('.srt-chart-card');
    const title = chartCard?.querySelector('.srt-chart-title');
    if (title) title.textContent = 'Network Throughput (kbps)';
  } else {
    // Demo data for video analyzer or when not connected
    drawChart('rttChart', '#00d4aa', i => 40 + Math.sin(i * 0.3 + time) * 15 + Math.random() * 5, 100, ' ms');
    drawChart('bwChart', '#4ade80', i => 7 + Math.sin(i * 0.2 + time * 0.5) * 2 + Math.random() * 0.5, 12, ' Mbps');
    drawChart('lossChart', '#f87171', i => Math.max(0, Math.sin(i * 0.5 + time) * 0.5 + Math.random() * 0.3), 2, '%');
    drawChart('bufChart', '#fbbf24', i => 100 + Math.sin(i * 0.15 + time * 0.3) * 30 + Math.random() * 10, 200, ' ms');
  }
}

function drawChart(canvasId, color, dataFn, maxVal, unit = '', windowSec = null, pointCount = 50, rightToLeft = false) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;

  // Sync internal canvas resolution with CSS size to avoid stretching/blur
  let cssW = canvas.clientWidth;
  let cssH = canvas.clientHeight;
  // Fallback to prevent zero-size canvas if layout is not ready yet
  if (!cssW) cssW = 400;
  if (!cssH) cssH = windowSec ? 280 : 140;
  if (canvas.width !== cssW || canvas.height !== cssH) {
    canvas.width = cssW;
    canvas.height = cssH;
  }

  const ctx = canvas.getContext('2d');
  const w = canvas.width;
  const h = canvas.height;
  const padLeft = unit ? 52 : 10;
  const padRight = 10;
  const padTop = 12;
  const padBottom = windowSec ? 22 : 12;

  const chartW = w - padLeft - padRight;
  const chartH = h - padTop - padBottom;

  ctx.clearRect(0, 0, w, h);

  const data = [];
  for (let i = 0; i < pointCount; i++) {
    data.push(dataFn(i));
  }

  if (data.length < 2) {
    // Not enough data to draw a chart yet
    return;
  }

  const stepX = chartW / (data.length - 1);

  function getX(i) {
    return rightToLeft ? padLeft + chartW - i * stepX : padLeft + i * stepX;
  }

  // Grid & labels
  const gridLines = 4;
  ctx.strokeStyle = '#ffffff18';
  ctx.lineWidth = 1;
  ctx.font = '11px var(--font-mono), monospace';
  ctx.fillStyle = '#ffffff90';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';

  for (let g = 0; g <= gridLines; g++) {
    const y = padTop + (g / gridLines) * chartH;
    const val = maxVal * (1 - g / gridLines);

    // horizontal grid line
    ctx.beginPath();
    ctx.moveTo(padLeft, y);
    ctx.lineTo(padLeft + chartW, y);
    ctx.stroke();

    // value label
    if (unit) {
      let label;
      if (val >= 1000) {
        label = (val / 1000).toFixed(1) + 'k' + unit;
      } else if (val < 1 && val > 0) {
        label = val.toFixed(2) + unit;
      } else {
        label = Math.round(val) + unit;
      }
      ctx.fillText(label, padLeft - 8, y);
    }
  }

  // vertical grid lines (every 10 points)
  ctx.strokeStyle = '#ffffff0a';
  for (let i = 0; i < data.length; i += 10) {
    const x = getX(i);
    ctx.beginPath();
    ctx.moveTo(x, padTop);
    ctx.lineTo(x, padTop + chartH);
    ctx.stroke();
  }

  // X-axis labels (time window)
  if (windowSec) {
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    ctx.fillStyle = '#ffffff80';
    ctx.font = '10px var(--font-mono), monospace';

    function formatTime(sec) {
      if (sec === 0) return 'now';
      if (windowSec < 120) return '-' + sec + 's';
      return '-' + (sec / 60).toFixed(sec % 60 === 0 ? 0 : 1) + 'm';
    }

    const midSec = windowSec / 2;
    const xLabels = [
      { x: padLeft + chartW, text: 'now' },
      { x: padLeft + chartW * 0.5, text: formatTime(midSec) },
      { x: padLeft, text: formatTime(windowSec) }
    ];
    xLabels.forEach(item => {
      ctx.fillText(item.text, item.x, padTop + chartH + 6);
    });

    // X-axis baseline
    ctx.strokeStyle = '#ffffff30';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padLeft, padTop + chartH);
    ctx.lineTo(padLeft + chartW, padTop + chartH);
    ctx.stroke();
  }

  // Fill
  ctx.beginPath();
  for (let i = 0; i < data.length; i++) {
    const x = getX(i);
    const y = padTop + chartH - (data[i] / maxVal) * chartH;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.lineTo(padLeft + chartW, padTop + chartH);
  ctx.lineTo(padLeft, padTop + chartH);
  ctx.closePath();
  ctx.fillStyle = color + '22';
  ctx.fill();

  // Line
  ctx.beginPath();
  for (let i = 0; i < data.length; i++) {
    const x = getX(i);
    const y = padTop + chartH - (data[i] / maxVal) * chartH;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.stroke();
}

function drawNetworkThroughputChart() {
  const canvas = document.getElementById('bwChart');
  if (!canvas) return;

  let cssW = canvas.clientWidth;
  let cssH = canvas.clientHeight;
  // Fallback to prevent zero-size canvas if layout is not ready yet
  if (!cssW) cssW = 800;
  if (!cssH) cssH = 280;
  if (canvas.width !== cssW || canvas.height !== cssH) {
    canvas.width = cssW;
    canvas.height = cssH;
  }

  const ctx = canvas.getContext('2d');
  const w = canvas.width;
  const h = canvas.height;

  const padLeft = 52;
  const padRight = 10;
  const padTop = 12;
  const padBottom = 22;
  const chartW = Math.max(1, w - padLeft - padRight);
  const chartH = Math.max(1, h - padTop - padBottom);

  ctx.clearRect(0, 0, w, h);

  const history = state.throughputHistory;
  const windowSec = state.networkWindow || 300;
  // Bitrate samples arrive roughly every 2 seconds
  const totalPoints = Math.max(2, Math.ceil(windowSec * 0.5));
  const windowPoints = Math.min(history.length, totalPoints);

  if (windowPoints < 2) {
    ctx.fillStyle = '#ffffff80';
    ctx.font = '12px var(--font-mono), monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('Collecting data...', w / 2, h / 2);
    return;
  }

  const visibleHistory = history.slice(-windowPoints).reverse();
  const maxVal = Math.max(...visibleHistory, 500);
  // Fixed step based on full window so points scroll smoothly right-to-left
  const stepX = chartW / (totalPoints - 1);

  // Horizontal grid & Y labels
  const gridLines = 4;
  ctx.strokeStyle = '#ffffff18';
  ctx.lineWidth = 1;
  ctx.font = '11px var(--font-mono), monospace';
  ctx.fillStyle = '#ffffff90';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';

  for (let g = 0; g <= gridLines; g++) {
    const y = padTop + (g / gridLines) * chartH;
    const val = maxVal * (1 - g / gridLines);

    ctx.beginPath();
    ctx.moveTo(padLeft, y);
    ctx.lineTo(padLeft + chartW, y);
    ctx.stroke();

    let label;
    if (val >= 1000) {
      label = (val / 1000).toFixed(1) + 'k kbps';
    } else if (val < 1 && val > 0) {
      label = val.toFixed(2) + ' kbps';
    } else {
      label = Math.round(val) + ' kbps';
    }
    ctx.fillText(label, padLeft - 8, y);
  }

  // Vertical grid lines (every ~10 points, based on full window)
  ctx.strokeStyle = '#ffffff0a';
  for (let i = 0; i < totalPoints; i += 10) {
    const x = padLeft + chartW - i * stepX;
    if (x < padLeft) break;
    ctx.beginPath();
    ctx.moveTo(x, padTop);
    ctx.lineTo(x, padTop + chartH);
    ctx.stroke();
  }

  // X-axis labels (time window)
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  ctx.fillStyle = '#ffffff80';
  ctx.font = '10px var(--font-mono), monospace';

  function formatTime(sec) {
    if (sec === 0) return 'now';
    if (windowSec < 120) return '-' + sec + 's';
    return '-' + (sec / 60).toFixed(sec % 60 === 0 ? 0 : 1) + 'm';
  }

  const midSec = windowSec / 2;
  const xLabels = [
    { x: padLeft + chartW, text: 'now' },
    { x: padLeft + chartW * 0.5, text: formatTime(midSec) },
    { x: padLeft, text: formatTime(windowSec) }
  ];
  xLabels.forEach(item => {
    ctx.fillText(item.text, item.x, padTop + chartH + 6);
  });

  // X-axis baseline
  ctx.strokeStyle = '#ffffff30';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padLeft, padTop + chartH);
  ctx.lineTo(padLeft + chartW, padTop + chartH);
  ctx.stroke();

  // Line (right-to-left: newest on the right)
  let lastX = padLeft + chartW;
  ctx.beginPath();
  for (let i = 0; i < windowPoints; i++) {
    const x = padLeft + chartW - i * stepX;
    const y = padTop + chartH - (visibleHistory[i] / maxVal) * chartH;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
    lastX = x;
  }
  ctx.strokeStyle = '#4ade80';
  ctx.lineWidth = 2;
  ctx.stroke();

  // Fill area under the line (right-to-left)
  ctx.lineTo(lastX, padTop + chartH);
  ctx.lineTo(padLeft + chartW, padTop + chartH);
  ctx.closePath();
  ctx.fillStyle = 'rgba(74, 222, 128, 0.1)';
  ctx.fill();
}

// ============================================
// Canvas Drawing (Legacy + New)
// ============================================

function drawSpectrum() {
  const canvas = document.getElementById('spectrum-canvas');
  if (!canvas) return;

  // Sync internal canvas resolution with CSS size to avoid stretching/blur
  let cssW = canvas.clientWidth;
  let cssH = canvas.clientHeight;
  if (!cssW) cssW = 400;
  if (!cssH) cssH = 160;
  if (canvas.width !== cssW || canvas.height !== cssH) {
    canvas.width = cssW;
    canvas.height = cssH;
  }

  const ctx = canvas.getContext('2d');
  const w = canvas.width;
  const h = canvas.height;
  const bands = state.metrics.spectrum.bands;

  ctx.clearRect(0, 0, w, h);

  ctx.strokeStyle = '#252532';
  ctx.lineWidth = 1;
  for (let i = 0; i < 5; i++) {
    const y = (h / 4) * i;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
  }

  const barW = (w - 20) / bands.length;
  const colors = { safe: '#4ade80', caution: '#fbbf24', warning: '#fb923c', danger: '#f87171' };
  bands.forEach((db, i) => {
    const pct = dbfsToPercent(db) / 100;
    const barH = pct * (h - 20);
    const x = 10 + i * barW;
    const y = h - 10 - barH;
    ctx.fillStyle = colors[dbfsToColor(db)] || '#4ade80';
    ctx.fillRect(x + 1, y, barW - 2, barH);
  });
}

function drawHistory() {
  const canvas = document.getElementById('history-canvas') || document.getElementById('historyCanvas');
  if (!canvas) return;

  // Sync internal canvas resolution with CSS size to avoid stretching/blur
  let cssW = canvas.clientWidth;
  let cssH = canvas.clientHeight;
  // Fallback to prevent zero-size canvas if layout is not ready yet
  if (!cssW) cssW = 800;
  if (!cssH) cssH = 320;
  if (canvas.width !== cssW || canvas.height !== cssH) {
    canvas.width = cssW;
    canvas.height = cssH;
  }

  const ctx = canvas.getContext('2d');
  const w = canvas.width;
  const h = canvas.height;
  const values = state.metrics.history;
  const windowSec = state.lufsHistoryWindow || 300;
  // Each history point represents 1 second
  const windowPoints = Math.min(values.length, windowSec);
  const windowValues = values.slice(-windowPoints);

  const padLeft = 44;
  const padRight = 10;
  const padTop = 16;
  const padBottom = 20;
  const chartW = w - padLeft - padRight;
  const chartH = h - padTop - padBottom;

  ctx.clearRect(0, 0, w, h);

  function dbToY(db) {
    return padTop + chartH - (lufsHistoryToPercent(db) / 100) * chartH;
  }

  // Y-axis grid & labels (-50 to 0 LUFS)
  ctx.font = '11px var(--font-mono), monospace';
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  ctx.fillStyle = '#ffffff90';
  ctx.strokeStyle = '#ffffff18';
  ctx.lineWidth = 1;

  const yLabels = [0, -10, -20, -23, -30, -40, -50];
  yLabels.forEach(db => {
    const y = dbToY(db);

    // grid line
    ctx.beginPath();
    ctx.moveTo(padLeft, y);
    ctx.lineTo(padLeft + chartW, y);
    ctx.stroke();

    // label
    ctx.fillText(db.toString(), padLeft - 8, y);
  });

  // Target line at -23 LUFS
  const targetY = dbToY(-23);
  ctx.strokeStyle = '#4a9eff';
  ctx.setLineDash([5, 5]);
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(padLeft, targetY);
  ctx.lineTo(padLeft + chartW, targetY);
  ctx.stroke();
  ctx.setLineDash([]);

  // X-axis labels (time) based on selected window
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  ctx.fillStyle = '#ffffff80';
  ctx.font = '10px var(--font-mono), monospace';

  function formatTime(sec) {
    if (sec === 0) return 'now';
    if (windowSec < 120) return '-' + sec + 's';
    return '-' + (sec / 60).toFixed(sec % 60 === 0 ? 0 : 1) + 'm';
  }

  const midSec = windowSec / 2;
  const xLabels = [
    { x: padLeft + chartW, text: 'now' },
    { x: padLeft + chartW * 0.5, text: formatTime(midSec) },
    { x: padLeft, text: formatTime(windowSec) }
  ];
  xLabels.forEach(item => {
    ctx.fillText(item.text, item.x, padTop + chartH + 6);
  });

  // X-axis baseline
  ctx.strokeStyle = '#ffffff30';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padLeft, padTop + chartH);
  ctx.lineTo(padLeft + chartW, padTop + chartH);
  ctx.stroke();

  if (windowValues.length < 2) return;

  // Draw loudness line for the selected window
  ctx.strokeStyle = '#4ade80';
  ctx.lineWidth = 2;
  ctx.beginPath();
  windowValues.forEach((db, i) => {
    const x = padLeft + (i / (windowValues.length - 1)) * chartW;
    const y = dbToY(db);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Fill area under the line
  ctx.fillStyle = 'rgba(74, 222, 128, 0.1)';
  ctx.lineTo(padLeft + chartW, padTop + chartH);
  ctx.lineTo(padLeft, padTop + chartH);
  ctx.closePath();
  ctx.fill();
}

// ============================================
// Alerts
// ============================================

function addAlert(severity, message) {
  const container = document.getElementById('alerts-container');
  if (!container) return;

  const now = new Date();
  const time = now.getHours().toString().padStart(2, '0') + ':' +
               now.getMinutes().toString().padStart(2, '0') + ':' +
               now.getSeconds().toString().padStart(2, '0');

  const item = document.createElement('div');
  item.className = 'alert-item ' + severity;
  item.innerHTML = `
    <div>
      <div class="alert-time">${time}</div>
      <div class="alert-msg">${message}</div>
    </div>
  `;
  container.insertBefore(item, container.firstChild);
}

// ============================================
// Helpers
// ============================================

function dbfsToPercent(db) {
  return Math.max(0, Math.min(100, (db + 70) / 70 * 100));
}

function lufsToPercent(lufs) {
  return Math.max(0, Math.min(100, (lufs + 70) / 70 * 100));
}

function lufsHistoryToPercent(lufs) {
  // Loudness history graph scale: -50 LUFS (bottom) to 0 LUFS (top)
  return Math.max(0, Math.min(100, (lufs + 50) / 50 * 100));
}

function dbfsToColor(db) {
  if (db >= -6) return 'danger';
  if (db >= -9) return 'warning';
  if (db >= -18) return 'caution';
  return 'safe';
}

function lufsToColor(lufs) {
  if (lufs > -14) return 'danger';
  if (lufs >= -24 && lufs <= -22) return 'safe';
  if (lufs < -40) return 'safe';
  return 'caution';
}

function setHeight(id, pct) {
  const el = document.getElementById(id);
  if (el) el.style.height = pct + '%';
}

function setWidth(id, pct) {
  const el = document.getElementById(id);
  if (el) el.style.width = pct + '%';
}

function setColor(id, colorClass) {
  const el = document.getElementById(id);
  if (el) {
    el.className = el.className.replace(/\b(safe|caution|warning|danger)\b/g, '');
    el.classList.add(colorClass);
  }
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

// Expose for inline scripts
window.initAnalyzer = initAnalyzer;
