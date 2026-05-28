/* ============================================================
   Network-Aware Adaptive IoT Dashboard – client logic
   - Polls the Flask server every POLL_MS for the latest readings
   - Updates 6 Chart.js line charts, 6 KPI tiles, and an alert log
   ============================================================ */

const POLL_MS    = 1000;   // dashboard refresh cadence (1 Hz)
const MAX_POINTS = 60;     // sliding window of points kept in each chart

const COLORS = {
  rtt:        '#5b8bff',
  loss:       '#e74c3c',
  rssi:       '#2ecc71',
  throughput: '#f1c40f',
  interval:   '#9b59b6',
  packetSize: '#1abc9c',
};

/* ------------------------------------------------------------
   Chart factory
   ------------------------------------------------------------ */
function makeChart(id, color, label) {
  const ctx = document.getElementById(id).getContext('2d');
  return new Chart(ctx, {
    type: 'line',
    data: {
      labels: [],
      datasets: [{
        label,
        data: [],
        borderColor: color,
        backgroundColor: color + '22',
        fill: true,
        tension: 0.30,
        pointRadius: 0,
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          mode: 'index',
          intersect: false,
          backgroundColor: '#0c1130',
          borderColor: '#2a335f',
          borderWidth: 1,
        },
      },
      scales: {
        x: {
          ticks: { color: '#8a93c4', maxRotation: 0, maxTicksLimit: 6 },
          grid:  { color: '#2a335f33' },
        },
        y: {
          ticks: { color: '#8a93c4' },
          grid:  { color: '#2a335f' },
        },
      },
    },
  });
}

const charts = {
  rtt:        makeChart('chartRtt',        COLORS.rtt,        'RTT'),
  loss:       makeChart('chartLoss',       COLORS.loss,       'Loss'),
  rssi:       makeChart('chartRssi',       COLORS.rssi,       'RSSI'),
  throughput: makeChart('chartThroughput', COLORS.throughput, 'Throughput'),
  interval:   makeChart('chartInterval',   COLORS.interval,   'Interval'),
  packetSize: makeChart('chartPacketSize', COLORS.packetSize, 'Packet size'),
};

/* ------------------------------------------------------------
   Helpers
   ------------------------------------------------------------ */
const $ = (id) => document.getElementById(id);
const logsEl = $('logs');
let lastSeenId  = null;
let lastReading = null;   // most recent telemetry row, kept for the link-status tick
let lastDeviceLinkOnline   = null;  // last *logged* state (null = no log yet)
let pendingDeviceLinkOnline = null; // candidate state waiting to be confirmed
let pendingDeviceLinkSince  = 0;    // ms timestamp the candidate first appeared

function fmt(num, digits = 0) {
  if (num === null || num === undefined || Number.isNaN(Number(num))) return '—';
  return Number(num).toFixed(digits);
}

function pushPoint(chart, label, value) {
  chart.data.labels.push(label);
  chart.data.datasets[0].data.push(value);
  while (chart.data.datasets[0].data.length > MAX_POINTS) {
    chart.data.datasets[0].data.shift();
    chart.data.labels.shift();
  }
  chart.update('none');
}

function pushLog(line, cls = 'info') {
  const div = document.createElement('div');
  div.className = 'log-line ' + cls;
  div.textContent = `[${new Date().toLocaleTimeString()}] ${line}`;
  logsEl.prepend(div);
  while (logsEl.children.length > 200) logsEl.removeChild(logsEl.lastChild);
}

function setServerStatus(online) {
  const el = $('serverStatus');
  el.className = 'kpi-value status-dot ' + (online ? 'online' : 'offline');
  el.textContent = online ? '● online' : '● offline';
}

/* ------------------------------------------------------------
   Device link status: derived from the age of the most recent
   telemetry row. We treat the device as offline once we've
   missed more than ~2 transmission intervals (with a 15 s floor).
   ------------------------------------------------------------ */
// Require the new state to persist this long before we log a transition.
// Prevents one-tick blips during chaos / packet-loss bursts from flooding logs.
const LINK_TRANSITION_DEBOUNCE_MS = 5000;

function refreshDeviceLink() {
  const el = $('deviceLink');
  if (!lastReading || !lastReading.timestamp) {
    el.className = 'kpi-value status-dot';
    el.textContent = '● awaiting…';
    return;
  }
  const ageMs       = Math.max(0, Date.now() - lastReading.timestamp * 1000);
  const ageSec      = Math.round(ageMs / 1000);
  const intervalMs  = lastReading.interval || 5000;
  // Tolerate up to ~3 consecutive drops before declaring offline.
  const staleAfter  = Math.max(30000, 4 * intervalMs + 10000);
  const online      = ageMs <= staleAfter;

  el.className = 'kpi-value status-dot ' + (online ? 'online' : 'offline');
  el.textContent = online ? '● online' : '● offline';

  // First ever observation: record silently, never log.
  if (lastDeviceLinkOnline === null) {
    lastDeviceLinkOnline = online;
    pendingDeviceLinkOnline = online;
    return;
  }

  // No change from the logged state -> clear any pending candidate, done.
  if (online === lastDeviceLinkOnline) {
    pendingDeviceLinkOnline = online;
    pendingDeviceLinkSince  = 0;
    return;
  }

  // We have a candidate change. Require it to persist before logging.
  const now = Date.now();
  if (pendingDeviceLinkOnline !== online) {
    pendingDeviceLinkOnline = online;
    pendingDeviceLinkSince  = now;
    return;
  }
  if (now - pendingDeviceLinkSince < LINK_TRANSITION_DEBOUNCE_MS) {
    return;  // still waiting for the candidate to stabilize
  }

  // Candidate has held long enough -> log the transition exactly once.
  pushLog(
    online
      ? `Device ${lastReading.deviceId} link RESTORED`
      : `Device ${lastReading.deviceId} link LOST (no packet for ${ageSec}s)`,
    online ? 'good' : 'alert'
  );
  lastDeviceLinkOnline = online;
}

/* ------------------------------------------------------------
   Append a single reading to charts (and optionally log it)
   ------------------------------------------------------------ */
function plotReading(r, withLog) {
  const t = new Date(r.timestamp * 1000).toLocaleTimeString();
  pushPoint(charts.rtt,        t, r.rttMs);
  pushPoint(charts.loss,       t, r.lossPct);
  pushPoint(charts.rssi,       t, r.rssi);
  pushPoint(charts.throughput, t, r.throughputKbps);
  pushPoint(charts.interval,   t, r.interval);
  pushPoint(charts.packetSize, t, r.packetSize);

  if (withLog) {
    const cls = r.state === 'Poor'     ? 'alert'
              : r.state === 'Moderate' ? 'warn'
              : r.state === 'Good'     ? 'good'
              : 'info';
    pushLog(
      `${r.deviceId}  seq=${r.seq}  state=${r.state}  ` +
      `rtt=${fmt(r.rttMs,0)}ms  loss=${fmt(r.lossPct,1)}%  ` +
      `rssi=${r.rssi}dBm  th=${fmt(r.throughputKbps,2)}Kbps  ` +
      `→ int=${r.interval}ms  size=${r.packetSize}B`,
      cls
    );
  }
}

/* ------------------------------------------------------------
   Update top KPI tiles + status row from the latest reading
   ------------------------------------------------------------ */
function updateKpis(latest, totalRecords) {
  $('deviceId').textContent   = latest.deviceId || '—';
  $('rtt').textContent        = fmt(latest.rttMs, 0);
  $('loss').textContent       = fmt(latest.lossPct, 1);
  $('rssi').textContent       = fmt(latest.rssi, 0);
  $('throughput').textContent = fmt(latest.throughputKbps, 2);
  $('interval').textContent   = fmt(latest.interval, 0);
  $('packetSize').textContent = fmt(latest.packetSize, 0);

  const stateEl = $('state');
  stateEl.textContent = latest.state || '—';
  stateEl.className = 'kpi-value badge ' + (latest.state || '');

  $('lastUpdate').textContent = latest.timestamp
    ? new Date(latest.timestamp * 1000).toLocaleTimeString()
    : '—';

  $('recordCount').textContent = `${totalRecords} records`;
}

/* ------------------------------------------------------------
   Poll loop
   ------------------------------------------------------------ */
async function poll() {
  try {
    const res = await fetch('/metrics?limit=' + MAX_POINTS, { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const rows = await res.json();
    setServerStatus(true);

    if (!Array.isArray(rows) || rows.length === 0) return;

    if (lastSeenId === null) {
      // Initial backfill: wipe and plot the whole window without logging.
      Object.values(charts).forEach((c) => {
        c.data.labels = [];
        c.data.datasets[0].data = [];
      });
      rows.forEach((r) => plotReading(r, false));
      pushLog(`Loaded ${rows.length} historical readings`, 'info');
    } else {
      rows.filter((r) => r.id > lastSeenId).forEach((r) => plotReading(r, true));
    }
    lastSeenId = rows[rows.length - 1].id;

    // Pull total record count from /health to display next to the log panel.
    let total = rows.length;
    try {
      const h = await fetch('/health', { cache: 'no-store' });
      if (h.ok) total = (await h.json()).records ?? total;
    } catch (_) { /* ignore */ }

    lastReading = rows[rows.length - 1];
    updateKpis(lastReading, total);
    refreshDeviceLink();
  } catch (e) {
    setServerStatus(false);
    pushLog('Server unreachable: ' + e.message, 'alert');
    refreshDeviceLink();  // ages the "stale" counter even when server is down
  }
}

/* ------------------------------------------------------------
   Demo / chaos-injection controls
   ------------------------------------------------------------ */
const CHAOS_LEVELS = {
  healthy:   { delay_ms: 0,   drop_pct: 0,  cls: 'good' },
  congested: { delay_ms: 350, drop_pct: 20, cls: 'warn' },
  severe:    { delay_ms: 700, drop_pct: 40, cls: 'bad'  },
};

function setChaosPill(state) {
  $('chaosPill').textContent =
    `delay ${state.delay_ms} ms / drop ${Number(state.drop_pct).toFixed(0)} %`;
}

function highlightActive(level) {
  document.querySelectorAll('.demo-btn').forEach((b) => {
    b.classList.remove('active', 'good', 'warn', 'bad');
    b.classList.add(CHAOS_LEVELS[b.dataset.level].cls);
  });
  if (level) {
    const btn = document.querySelector(`.demo-btn[data-level="${level}"]`);
    if (btn) btn.classList.add('active', CHAOS_LEVELS[level].cls);
  }
}

async function applyChaos(level) {
  const params = CHAOS_LEVELS[level];
  try {
    const res = await fetch('/chaos', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ delay_ms: params.delay_ms, drop_pct: params.drop_pct }),
    });
    const state = await res.json();
    setChaosPill(state);
    highlightActive(level);
    const cls = level === 'healthy' ? 'good' : level === 'congested' ? 'warn' : 'alert';
    pushLog(
      `Injection set to ${level.toUpperCase()} ` +
      `(delay=${state.delay_ms}ms, drop=${Number(state.drop_pct).toFixed(0)}%)`,
      cls
    );
  } catch (e) {
    pushLog('Failed to update injection: ' + e.message, 'alert');
  }
}

document.querySelectorAll('.demo-btn').forEach((btn) => {
  btn.addEventListener('click', () => applyChaos(btn.dataset.level));
});

async function loadChaosState() {
  try {
    const res = await fetch('/chaos', { cache: 'no-store' });
    if (!res.ok) return;
    const state = await res.json();
    setChaosPill(state);
    // Reflect current state on the buttons
    let level = 'healthy';
    if (state.delay_ms >= 600 || state.drop_pct >= 35) level = 'severe';
    else if (state.delay_ms > 0 || state.drop_pct > 0) level = 'congested';
    highlightActive(level);
  } catch (_) { /* ignore */ }
}

/* Kick off */
loadChaosState();
poll();
setInterval(poll, POLL_MS);
setInterval(refreshDeviceLink, 1000);  // smooth "Xs ago" counter
