(() => {
  'use strict';

  const JWT_KEY = 'handler_panel_jwt';
  const AUTH_EXPIRED_ERROR = 'auth-expired';
  const views = ['dashboard', 'stats', 'devices', 'commands', 'settings'];
  const MAX_BREADCRUMBS = 6;

  const state = {
    role: null,
    selectedDeviceId: null,
    ws: null,
    wsReconnectTimer: null,
    wsOfflineVisualTimer: null,
    wsLastOnlineAt: 0,
    devices: {},
    feed: [],
    locationHistory: {},
    map: {
      instance: null,
      marker: null,
      trail: null,
      trailMarkers: [],
      autoFollow: true,
    },
    intelligence: {
      alerts: [],
      transport: {
        mqtt: 0,
        wsFallback: 0,
        failures: 0,
        recent: [],
      },
      staleRisk: [],
      behavior: {
        eventCount: 0,
        highRiskCount: 0,
        moodDelta: null,
        signals: [],
      },
    },
  };

  function byId(id) {
    return document.getElementById(id);
  }

  function setVisible(id, visible) {
    const el = byId(id);
    if (!el) return;
    el.hidden = !visible;
    el.classList.toggle('hp2-hidden', !visible);
  }

  function saveJwt(token) {
    sessionStorage.setItem(JWT_KEY, token);
  }

  function getJwt() {
    return sessionStorage.getItem(JWT_KEY) || '';
  }

  function clearJwt() {
    sessionStorage.removeItem(JWT_KEY);
  }

  function authHeader() {
    const jwt = getJwt();
    return jwt ? { Authorization: `Bearer ${jwt}` } : {};
  }

  async function apiFetch(path, options = {}) {
    const merged = {
      ...options,
      headers: {
        ...authHeader(),
        ...(options.headers || {}),
      },
    };
    const response = await fetch(path, merged);
    if (response.status === 401) {
      showLogin('Session expired.');
      throw new Error(AUTH_EXPIRED_ERROR);
    }
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      const message = body.detail || `Request failed (${response.status})`;
      throw new Error(message);
    }
    return response;
  }

  async function apiGet(path) {
    const response = await apiFetch(path);
    return response.json();
  }

  async function apiPost(path, body) {
    const response = await apiFetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    return response.json().catch(() => ({}));
  }

  function setWsOnline(online) {
    const pill = byId('hp2-ws-pill');

    const applyState = (nextOnline) => {
      pill.textContent = nextOnline ? 'WS ONLINE' : 'WS OFFLINE';
      pill.classList.toggle('hp2-pill-online', nextOnline);
      pill.classList.toggle('hp2-pill-offline', !nextOnline);
      if (nextOnline) {
        state.wsLastOnlineAt = Date.now();
      }
    };

    if (online) {
      if (state.wsOfflineVisualTimer) {
        clearTimeout(state.wsOfflineVisualTimer);
        state.wsOfflineVisualTimer = null;
      }
      applyState(true);
      return;
    }

    if (state.wsOfflineVisualTimer) {
      clearTimeout(state.wsOfflineVisualTimer);
    }
    const elapsed = Date.now() - (state.wsLastOnlineAt || 0);
    const delayMs = elapsed < 1200 ? 1200 - elapsed : 250;
    state.wsOfflineVisualTimer = setTimeout(() => {
      applyState(false);
      state.wsOfflineVisualTimer = null;
    }, delayMs);
  }

  function showLogin(message = '') {
    disconnectWs();
    setVisible('hp2-app', false);
    setVisible('hp2-login', true);
    byId('hp2-login-error').textContent = message;
    clearJwt();
  }

  function showApp() {
    setVisible('hp2-login', false);
    setVisible('hp2-app', true);
  }

  function pushFeed(text) {
    state.feed.unshift({ text, at: new Date().toISOString() });
    state.feed = state.feed.slice(0, 12);
    const feedEl = byId('hp2-feed');
    if (!feedEl) return;
    feedEl.innerHTML = state.feed
      .map((item) => `<li><div>${escapeHtml(item.text)}</div><div class="hp2-muted">${fmtDate(item.at)}</div></li>`)
      .join('');
  }

  function escapeHtml(value) {
    return String(value || '')
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  function fmtDate(iso) {
    if (!iso) return '-';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '-';
    return d.toLocaleString();
  }

  function setActiveView(viewName) {
    views.forEach((view) => {
      byId(`hp2-view-${view}`).classList.toggle('hp2-view-active', view === viewName);
    });
    document.querySelectorAll('.hp2-tab-btn').forEach((button) => {
      button.classList.toggle('hp2-tab-active', button.dataset.view === viewName);
    });
  }

  function renderKpis() {
    const devices = Object.values(state.devices);
    const known = devices.length;
    const online = devices.filter((d) => deviceOnline(d)).length;
    const locked = devices.filter((d) => Number(d.is_locked || 0) === 1).length;
    const batteryRows = devices.map((d) => Number(d.battery_pct)).filter((v) => Number.isFinite(v));
    const avgBattery = batteryRows.length
      ? `${Math.round(batteryRows.reduce((acc, v) => acc + v, 0) / batteryRows.length)}%`
      : '-';

    byId('hp2-kpi-known').textContent = String(known);
    byId('hp2-kpi-online').textContent = String(online);
    byId('hp2-kpi-locked').textContent = String(locked);
    byId('hp2-kpi-battery').textContent = avgBattery;
  }

  function renderDeviceList() {
    const container = byId('hp2-device-list');
    const devices = Object.values(state.devices).sort((a, b) => {
      const aMs = Date.parse(a.last_seen || '') || 0;
      const bMs = Date.parse(b.last_seen || '') || 0;
      return bMs - aMs;
    });
    if (!devices.length) {
      container.innerHTML = '<li class="hp2-muted">No devices available yet.</li>';
      return;
    }

    container.innerHTML = devices
      .map((d) => {
        const selected = d.device_id === state.selectedDeviceId;
        const statusLabel = deviceOnline(d) ? 'online' : 'offline';
        const battery = Number.isFinite(Number(d.battery_pct)) ? `${Number(d.battery_pct)}%` : '-';
        return `<li>
          <button type="button" data-device-id="${escapeHtml(d.device_id || '')}">
            <strong>${escapeHtml(d.device_name || d.device_id || 'Unknown')}</strong>
            <div class="hp2-device-meta">
              <span>${escapeHtml(statusLabel)}</span>
              <span>${escapeHtml(battery)}</span>
              <span>${selected ? 'selected' : ''}</span>
            </div>
          </button>
        </li>`;
      })
      .join('');
  }

  function renderSelectedDevice() {
    const d = state.selectedDeviceId ? state.devices[state.selectedDeviceId] : null;
    byId('hp2-title').textContent = d ? (d.device_name || d.device_id || 'Selected Device') : 'No Device Selected';
    byId('hp2-detail-id').textContent = d?.device_id || '-';
    byId('hp2-detail-name').textContent = d?.device_name || '-';
    byId('hp2-detail-online').textContent = d ? (deviceOnline(d) ? 'Online' : 'Offline') : '-';
    byId('hp2-detail-lock').textContent = d ? (Number(d.is_locked || 0) === 1 ? 'Locked' : 'Unlocked') : '-';
    byId('hp2-detail-battery').textContent = d && Number.isFinite(Number(d.battery_pct)) ? `${d.battery_pct}%` : '-';
    byId('hp2-detail-last').textContent = d ? fmtDate(d.last_seen) : '-';
    byId('hp2-dashboard-battery').textContent = d && Number.isFinite(Number(d.battery_pct)) ? `${d.battery_pct}%` : '-';
    byId('hp2-dashboard-connection').textContent = d ? (deviceOnline(d) ? 'Connected' : 'Offline') : '-';
    renderDashboardAlerts();
    renderLiveMap();
  }

  function ingestLocation(device) {
    if (!device || !device.device_id) return;
    const lat = Number(device.lat);
    const lon = Number(device.lon);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;

    const key = device.device_id;
    if (!state.locationHistory[key]) {
      state.locationHistory[key] = [];
    }
    const history = state.locationHistory[key];
    const last = history[history.length - 1];
    if (last && Math.abs(last.lat - lat) < 0.00001 && Math.abs(last.lon - lon) < 0.00001) {
      return;
    }
    history.push({ lat, lon, at: device.last_seen || new Date().toISOString() });
    if (history.length > MAX_BREADCRUMBS) {
      history.splice(0, history.length - MAX_BREADCRUMBS);
    }
  }

  function ensureMapReady() {
    if (state.map.instance) return state.map.instance;
    const mapEl = byId('hp2-live-map');
    if (!mapEl || typeof window.L === 'undefined') return null;

    state.map.instance = window.L.map(mapEl, { zoomControl: true, attributionControl: true });
    window.L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap contributors',
    }).addTo(state.map.instance);
    state.map.instance.setView([37.7749, -122.4194], 12);
    return state.map.instance;
  }

  function renderLiveMap() {
    const listEl = byId('hp2-location-breadcrumbs');
    if (!listEl) return;

    const deviceId = state.selectedDeviceId;
    const history = deviceId ? (state.locationHistory[deviceId] || []) : [];

    if (!history.length) {
      listEl.innerHTML = '<li class="hp2-muted">No location points yet.</li>';
      return;
    }

    const map = ensureMapReady();
    if (map) {
      if (state.map.marker) {
        map.removeLayer(state.map.marker);
        state.map.marker = null;
      }
      if (state.map.trail) {
        map.removeLayer(state.map.trail);
        state.map.trail = null;
      }
      state.map.trailMarkers.forEach((marker) => map.removeLayer(marker));
      state.map.trailMarkers = [];

      const coords = history.map((p) => [p.lat, p.lon]);
      const latest = coords[coords.length - 1];
      state.map.marker = window.L.marker(latest).addTo(map);
      state.map.trail = window.L.polyline(coords, { color: '#ff8c42', weight: 4, opacity: 0.75 }).addTo(map);
      state.map.trailMarkers = coords.map((pt, idx) => {
        const marker = window.L.circleMarker(pt, {
          radius: idx === coords.length - 1 ? 6 : 4,
          color: idx === coords.length - 1 ? '#ffd166' : '#ffb071',
          fillColor: idx === coords.length - 1 ? '#ffd166' : '#ff8c42',
          fillOpacity: 0.85,
          weight: 1,
        }).addTo(map);
        return marker;
      });
      if (state.map.autoFollow) {
        map.setView(latest, 16, { animate: false });
      }
      setTimeout(() => map.invalidateSize(), 0);
    }

    const points = history.slice(-MAX_BREADCRUMBS).reverse();
    listEl.innerHTML = points
      .map((point, idx) => `<li>
          <div class="hp2-feed-item-row">
            <strong>Point ${history.length - idx}</strong>
            <span class="hp2-muted">${escapeHtml(fmtDate(point.at))}</span>
          </div>
          <div class="hp2-meta-mono">${point.lat.toFixed(5)}, ${point.lon.toFixed(5)}</div>
        </li>`)
      .join('');
  }

  function renderAutoFollowButton() {
    const btn = byId('hp2-autofollow-btn');
    if (!btn) return;
    btn.textContent = state.map.autoFollow ? 'Auto-follow: ON' : 'Auto-follow: OFF';
  }

  function toggleAutoFollow() {
    state.map.autoFollow = !state.map.autoFollow;
    renderAutoFollowButton();
    if (state.map.autoFollow) {
      renderLiveMap();
    }
  }

  function dashboardCriticalAlerts() {
    const alerts = Array.isArray(state.intelligence.alerts) ? state.intelligence.alerts : [];
    const filtered = alerts.filter((item) => item.level === 'critical' || item.level === 'warning');
    const selected = state.selectedDeviceId ? state.devices[state.selectedDeviceId] : null;
    if (selected && (selected.ai_alert === true || Number(selected.ai_alert || 0) === 1)) {
      filtered.unshift({
        title: 'AI alert',
        subtitle: selected.ai_label || 'AI flagged device behavior',
        at: selected.last_seen || new Date().toISOString(),
        level: 'critical',
      });
    }
    return filtered.slice(0, 6);
  }

  function renderDashboardAlerts() {
    const listEl = byId('hp2-alert-list');
    if (!listEl) return;
    const alerts = dashboardCriticalAlerts();
    if (!alerts.length) {
      listEl.innerHTML = '<li class="hp2-muted">No active warning/critical alerts.</li>';
      return;
    }
    listEl.innerHTML = alerts
      .map((item) => `<li>
          <div class="hp2-feed-item-row">
            <strong>${escapeHtml(item.title || 'Alert')}</strong>
            <span class="${severityClass(item.level || 'warning')}">${escapeHtml(item.level || 'warning')}</span>
          </div>
          <div class="hp2-muted">${escapeHtml(item.subtitle || '')}</div>
          <div class="hp2-muted">${escapeHtml(fmtDate(item.at))}</div>
        </li>`)
      .join('');
  }

  function deviceOnline(device) {
    if (!device) return false;
    if (Number(device.is_online || 0) === 1) return true;
    const seenMs = Date.parse(device.last_seen || '');
    if (!Number.isFinite(seenMs)) return false;
    return Date.now() - seenMs <= 5 * 60 * 1000;
  }

  async function loadDevices() {
    const list = await apiGet('/api/handler/devices');
    state.devices = {};
    (Array.isArray(list) ? list : []).forEach((d) => {
      state.devices[d.device_id] = { ...d };
      ingestLocation(d);
    });

    if (!state.selectedDeviceId || !state.devices[state.selectedDeviceId]) {
      const first = Object.values(state.devices)[0];
      state.selectedDeviceId = first?.device_id || null;
    }

    renderKpis();
    renderDeviceList();
    renderSelectedDevice();

    if (state.selectedDeviceId) {
      await refreshSelectedStatus();
    }
  }

  async function refreshSelectedStatus() {
    if (!state.selectedDeviceId) return;
    const status = await apiGet(`/api/handler/status?device_id=${encodeURIComponent(state.selectedDeviceId)}`);
    if (status && status.device_id) {
      state.devices[status.device_id] = {
        ...(state.devices[status.device_id] || {}),
        ...status,
      };
      ingestLocation(state.devices[status.device_id]);
      renderKpis();
      renderDeviceList();
      renderSelectedDevice();
    }
  }

  function connectWs() {
    const jwt = getJwt();
    if (!jwt) return;

    disconnectWs();
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${protocol}://${location.host}/ws/handler?token=${encodeURIComponent(jwt)}`;
    const ws = new WebSocket(url);
    state.ws = ws;

    ws.addEventListener('open', () => setWsOnline(true));
    ws.addEventListener('error', () => setWsOnline(false));
    ws.addEventListener('close', () => {
      setWsOnline(false);
      if (state.wsReconnectTimer) {
        clearTimeout(state.wsReconnectTimer);
      }
      state.wsReconnectTimer = setTimeout(connectWs, 2000);
    });

    ws.addEventListener('message', (evt) => {
      if (evt.data instanceof ArrayBuffer) {
        return;
      }
      let msg;
      try {
        msg = JSON.parse(evt.data);
      } catch (_e) {
        return;
      }
      if (!msg || msg.type === 'ping') return;

      if (msg.type === 'snapshot' && Array.isArray(msg.devices)) {
        msg.devices.forEach((d) => {
          state.devices[d.device_id] = {
            ...(state.devices[d.device_id] || {}),
            ...d,
          };
          ingestLocation(state.devices[d.device_id]);
        });
        renderKpis();
        renderDeviceList();
        renderSelectedDevice();
        pushFeed(`Snapshot received (${msg.devices.length} devices).`);
        return;
      }

      if (msg.type === 'status_update' && msg.device_id) {
        state.devices[msg.device_id] = {
          ...(state.devices[msg.device_id] || {}),
          ...msg,
        };
        ingestLocation(state.devices[msg.device_id]);
        renderKpis();
        renderDeviceList();
        renderSelectedDevice();
        pushFeed(`Status updated: ${msg.device_id}`);
        return;
      }

      if (msg.type === 'lock' && msg.device_id) {
        state.devices[msg.device_id] = {
          ...(state.devices[msg.device_id] || {}),
          is_locked: msg.is_locked !== undefined ? Number(msg.is_locked ? 1 : 0) : 1,
        };
        renderKpis();
        renderDeviceList();
        renderSelectedDevice();
        pushFeed(`Lock confirmed: ${msg.device_id}`);
        return;
      }

      if (msg.type === 'device_deleted' && msg.device_id) {
        delete state.devices[msg.device_id];
        if (state.selectedDeviceId === msg.device_id) {
          state.selectedDeviceId = null;
        }
        renderKpis();
        renderDeviceList();
        renderSelectedDevice();
        pushFeed(`Device removed: ${msg.device_id}`);
      }
    });
  }

  function disconnectWs() {
    if (state.ws) {
      state.ws.close();
      state.ws = null;
    }
    if (state.wsReconnectTimer) {
      clearTimeout(state.wsReconnectTimer);
      state.wsReconnectTimer = null;
    }
    if (state.wsOfflineVisualTimer) {
      clearTimeout(state.wsOfflineVisualTimer);
      state.wsOfflineVisualTimer = null;
    }
    setWsOnline(false);
  }

  async function login() {
    const user = byId('hp2-user').value.trim();
    const pass = byId('hp2-pass').value;
    const errorEl = byId('hp2-login-error');
    const button = byId('hp2-login-btn');

    errorEl.textContent = '';
    if (!user || !pass) {
      errorEl.textContent = 'Please enter credentials.';
      return;
    }

    button.disabled = true;
    button.textContent = 'Checking...';

    try {
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: user, password: pass }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        errorEl.textContent = body.detail || 'Invalid credentials.';
        return;
      }
      const data = await response.json();
      if (data.role !== 'handler' && data.role !== 'admin') {
        errorEl.textContent = 'Access denied: handler/admin role required.';
        return;
      }

      saveJwt(data.access_token);
      state.role = data.role;
      byId('hp2-role').textContent = data.role.toUpperCase();
      showApp();
      await hydrateApp();
    } catch (_e) {
      clearJwt();
      errorEl.textContent = 'Login failed. Please try again.';
    } finally {
      button.disabled = false;
      button.textContent = 'Enter V2 Panel';
    }
  }

  async function hydrateApp() {
    const jobs = [
      loadDevices(),
      loadQueueKpis(),
      loadDashboardIntelligence(),
    ];
    const results = await Promise.allSettled(jobs);
    connectWs();
    pushFeed('Session ready.');

    const failed = results.filter((r) => r.status === 'rejected');
    if (failed.length) {
      pushFeed(`Loaded with ${failed.length} warning(s).`);
      const result = byId('hp2-action-result');
      if (result) {
        result.textContent = 'Signed in with partial data. Use refresh if a panel is empty.';
      }
    }
  }

  function severityClass(level) {
    if (level === 'critical') return 'hp2-severity hp2-severity-critical';
    if (level === 'warning') return 'hp2-severity hp2-severity-warning';
    return 'hp2-severity hp2-severity-info';
  }

  function readPayloadJson(rawPayload) {
    if (!rawPayload) return {};
    if (typeof rawPayload === 'object') return rawPayload;
    try {
      return JSON.parse(rawPayload);
    } catch (_e) {
      return {};
    }
  }

  function minutesSince(iso) {
    const ms = Date.parse(iso || '');
    if (!Number.isFinite(ms)) return null;
    return Math.max(0, Math.round((Date.now() - ms) / 60000));
  }

  function deriveStaleRisk() {
    const rows = Object.values(state.devices).map((device) => {
      const mins = minutesSince(device.last_seen);
      const battery = Number(device.battery_pct);
      let severity = 'info';
      const notes = [];

      if (!Number.isFinite(mins) || mins >= 30) {
        severity = 'critical';
        notes.push('stale telemetry');
      } else if (mins >= 10) {
        severity = 'warning';
        notes.push('aging telemetry');
      }

      if (Number.isFinite(battery) && battery <= 20) {
        if (severity !== 'critical') severity = 'warning';
        notes.push(`low battery ${battery}%`);
      }

      if (Number(device.ai_alert || 0) === 1 || device.ai_alert === true) {
        severity = 'critical';
        notes.push('ai alert');
      }

      return {
        deviceId: device.device_id || 'unknown',
        label: device.device_name || device.device_id || 'Unknown',
        minutes: mins,
        severity,
        notes,
      };
    });

    rows.sort((a, b) => {
      const sevRank = { critical: 3, warning: 2, info: 1 };
      if (sevRank[b.severity] !== sevRank[a.severity]) {
        return sevRank[b.severity] - sevRank[a.severity];
      }
      return (b.minutes || 0) - (a.minutes || 0);
    });

    state.intelligence.staleRisk = rows.slice(0, 8);
  }

  function deriveTransportOutcomes(events) {
    let mqtt = 0;
    let wsFallback = 0;
    let failures = 0;

    const recent = [];
    (Array.isArray(events) ? events : []).forEach((eventRow) => {
      const payload = readPayloadJson(eventRow.payload_json);
      const reasonText = String(eventRow.reason || '').toLowerCase();
      const eventText = String(eventRow.event || '').toLowerCase();

      let transport = 'unknown';
      let level = 'info';

      if (payload.transport === 'mqtt' || Number(payload?.mqtt?.sent || 0) > 0) {
        transport = 'mqtt';
        mqtt += 1;
      } else if (
        payload.transport === 'ws_fallback' ||
        Number(payload?.ws_fallback?.sent || 0) > 0 ||
        reasonText.includes('ws_fallback')
      ) {
        transport = 'ws_fallback';
        wsFallback += 1;
        level = 'warning';
      }

      if (
        reasonText.includes('unavailable') ||
        reasonText.includes('failed') ||
        reasonText.includes('error') ||
        eventText.includes('failed')
      ) {
        failures += 1;
        level = 'critical';
      }

      if (recent.length < 8) {
        recent.push({
          at: eventRow.received_at,
          text: `${eventRow.event || 'event'} (${transport})`,
          reason: eventRow.reason || '',
          level,
        });
      }
    });

    state.intelligence.transport = {
      mqtt,
      wsFallback,
      failures,
      recent,
    };
  }

  function deriveAlertsTimeline(events, audits) {
    const timeline = [];

    (Array.isArray(events) ? events : []).slice(0, 16).forEach((row) => {
      const eventName = String(row.event || 'event');
      const reason = String(row.reason || 'No reason provided');
      let level = 'info';
      const reasonLower = reason.toLowerCase();

      if (eventName.toLowerCase().includes('punish') || reasonLower.includes('alert')) {
        level = 'warning';
      }
      if (reasonLower.includes('failed') || reasonLower.includes('error')) {
        level = 'critical';
      }

      timeline.push({
        at: row.received_at,
        title: eventName,
        subtitle: reason,
        level,
      });
    });

    (Array.isArray(audits) ? audits : []).slice(0, 12).forEach((row) => {
      const ratio = Number(row.detection_ratio);
      if (!Number.isFinite(ratio)) return;
      const level = ratio >= 0.6 ? 'critical' : ratio >= 0.35 ? 'warning' : 'info';
      timeline.push({
        at: row.received_at,
        title: `Audit ${row.last_label || 'signal'}`,
        subtitle: `Detection ratio ${(ratio * 100).toFixed(1)}%`,
        level,
      });
    });

    timeline.sort((a, b) => (Date.parse(b.at || '') || 0) - (Date.parse(a.at || '') || 0));
    state.intelligence.alerts = timeline.slice(0, 12);
  }

  function renderDashboardIntelligence() {
    const alertsEl = byId('hp2-alert-timeline');
    const transportEl = byId('hp2-transport-list');
    const staleEl = byId('hp2-stale-risk');
    const behaviorSignalsEl = byId('hp2-behavior-signals');

    byId('hp2-transport-mqtt').textContent = String(state.intelligence.transport.mqtt || 0);
    byId('hp2-transport-ws').textContent = String(state.intelligence.transport.wsFallback || 0);
    byId('hp2-transport-fail').textContent = String(state.intelligence.transport.failures || 0);

    if (!state.intelligence.alerts.length) {
      alertsEl.innerHTML = '<li class="hp2-muted">No alert events yet.</li>';
    } else {
      alertsEl.innerHTML = state.intelligence.alerts
        .map((item) => `<li>
            <div class="hp2-feed-item-row">
              <strong>${escapeHtml(item.title)}</strong>
              <span class="${severityClass(item.level)}">${escapeHtml(item.level)}</span>
            </div>
            <div class="hp2-muted">${escapeHtml(item.subtitle)}</div>
            <div class="hp2-muted">${escapeHtml(fmtDate(item.at))}</div>
          </li>`)
        .join('');
    }

    if (!state.intelligence.transport.recent.length) {
      transportEl.innerHTML = '<li class="hp2-muted">No transport outcomes yet.</li>';
    } else {
      transportEl.innerHTML = state.intelligence.transport.recent
        .map((item) => `<li>
            <div class="hp2-feed-item-row">
              <strong>${escapeHtml(item.text)}</strong>
              <span class="${severityClass(item.level)}">${escapeHtml(item.level)}</span>
            </div>
            <div class="hp2-muted">${escapeHtml(item.reason || 'No reason provided')}</div>
            <div class="hp2-muted">${escapeHtml(fmtDate(item.at))}</div>
          </li>`)
        .join('');
    }

    if (!state.intelligence.staleRisk.length) {
      staleEl.innerHTML = '<li class="hp2-muted">No stale risk data yet.</li>';
    } else {
      staleEl.innerHTML = state.intelligence.staleRisk
        .map((item) => `<li>
            <div class="hp2-feed-item-row">
              <strong>${escapeHtml(item.label)}</strong>
              <span class="${severityClass(item.severity)}">${escapeHtml(item.severity)}</span>
            </div>
            <div class="hp2-muted">${escapeHtml(item.deviceId)}</div>
            <div class="hp2-muted">${item.minutes != null ? `${item.minutes} min since last seen` : 'No last_seen timestamp'}</div>
            <div class="hp2-muted">${escapeHtml(item.notes.length ? item.notes.join(' | ') : 'No active risk flags')}</div>
          </li>`)
        .join('');
    }

    byId('hp2-behavior-events').textContent = String(state.intelligence.behavior.eventCount || 0);
    byId('hp2-behavior-risk').textContent = String(state.intelligence.behavior.highRiskCount || 0);
    byId('hp2-behavior-mood').textContent = state.intelligence.behavior.moodDelta == null
      ? '-'
      : String(state.intelligence.behavior.moodDelta);

    if (!state.intelligence.behavior.signals.length) {
      behaviorSignalsEl.innerHTML = '<li class="hp2-muted">No learning signals yet.</li>';
    } else {
      behaviorSignalsEl.innerHTML = state.intelligence.behavior.signals
        .map((signal) => `<li>
            <div class="hp2-feed-item-row">
              <strong>${escapeHtml(signal.title || 'Signal')}</strong>
              <span class="${severityClass(signal.level || 'info')}">${escapeHtml(signal.level || 'info')}</span>
            </div>
            <div class="hp2-muted">${escapeHtml(signal.value || '')}</div>
            <div class="hp2-muted hp2-meta-mono">${escapeHtml(signal.detail || '')}</div>
          </li>`)
        .join('');
    }
  }

  async function loadBehaviorPulse() {
    const qs = state.selectedDeviceId
      ? `?days=14&device_id=${encodeURIComponent(state.selectedDeviceId)}`
      : '?days=14';
    const data = await apiGet(`/api/handler/tpe/behavior-insights${qs}`).catch(() => null);
    if (!data) {
      state.intelligence.behavior = {
        eventCount: 0,
        highRiskCount: 0,
        moodDelta: null,
        signals: [],
      };
      return;
    }

    const moodDelta = data.mood_delta == null ? null : Number(data.mood_delta);
    const rawSignals = Array.isArray(data.learning_signals) ? data.learning_signals : [];
    const signals = rawSignals.slice(0, 6).map((signal) => {
      const detail = String(signal.detail || '').toLowerCase();
      let level = 'info';
      if (detail.includes('declin') || detail.includes('high-risk') || detail.includes('low-battery')) {
        level = 'warning';
      }
      if (detail.includes('ai alert') || detail.includes('pressure')) {
        level = 'critical';
      }
      return {
        title: signal.title || 'Signal',
        value: signal.value || '',
        detail: signal.detail || '',
        level,
      };
    });

    state.intelligence.behavior = {
      eventCount: Number(data.event_count || 0),
      highRiskCount: Number(data.high_risk_count || 0),
      moodDelta: Number.isFinite(moodDelta) ? moodDelta : null,
      signals,
    };
  }

  async function loadDashboardIntelligence() {
    const [events, audits] = await Promise.all([
      apiGet('/api/handler/tpe/events?limit=120').catch(() => []),
      apiGet('/api/handler/tpe/audits?limit=80').catch(() => []),
    ]);

    deriveTransportOutcomes(events);
    deriveAlertsTimeline(events, audits);
    deriveStaleRisk();
    await loadBehaviorPulse();
    renderDashboardIntelligence();
    renderDashboardAlerts();
  }

  async function loadQueueKpis() {
    const [booking, mail, questions, limbo] = await Promise.all([
      apiGet('/api/handler/booking?status=new&limit=200').catch(() => []),
      apiGet('/api/handler/puppy-mail/threads?status=open&limit=200').catch(() => []),
      apiGet('/api/handler/questions').catch(() => []),
      apiGet('/api/handler/limbo?status=pending&limit=200').catch(() => []),
    ]);

    byId('hp2-kpi-booking').textContent = String(Array.isArray(booking) ? booking.length : 0);
    byId('hp2-kpi-mail').textContent = String(Array.isArray(mail) ? mail.length : 0);
    byId('hp2-kpi-questions').textContent = String(Array.isArray(questions) ? questions.length : 0);
    byId('hp2-kpi-limbo').textContent = String(Array.isArray(limbo) ? limbo.length : 0);
  }

  async function lockSelected() {
    const result = byId('hp2-action-result');
    if (!state.selectedDeviceId) {
      result.textContent = 'Select a device first.';
      return;
    }
    result.textContent = 'Sending lock...';
    try {
      await apiPost('/api/handler/lock', { device_id: state.selectedDeviceId });
      result.textContent = 'Lock sent.';
      pushFeed(`Lock command sent for ${state.selectedDeviceId}`);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        result.textContent = `Failed: ${err.message}`;
      }
    }
  }

  async function requestCheckin() {
    const result = byId('hp2-action-result');
    if (!state.selectedDeviceId) {
      result.textContent = 'Select a device first.';
      return;
    }
    result.textContent = 'Requesting check-in...';
    try {
      await apiPost('/api/handler/tpe/checkins/request', { device_id: state.selectedDeviceId });
      result.textContent = 'Check-in requested.';
      pushFeed(`Check-in requested for ${state.selectedDeviceId}`);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        result.textContent = `Failed: ${err.message}`;
      }
    }
  }

  async function sendToyCommand() {
    const result = byId('hp2-command-result');
    if (!state.selectedDeviceId) {
      result.textContent = 'Select a device first.';
      return;
    }

    const action = byId('hp2-toy-target').value;
    const mode = byId('hp2-toy-action').value.trim() || 'vibrate';
    const intensity = Number(byId('hp2-toy-intensity').value || 10);
    const length = Number(byId('hp2-toy-length').value || 800);

    const payload = {
      device_id: state.selectedDeviceId,
      action,
      payload: {
        command: mode,
        intensity,
        length,
        level: intensity,
        duration_ms: length,
      },
    };

    result.textContent = 'Sending command...';
    try {
      await apiPost('/api/handler/tpe/push', payload);
      result.textContent = 'Command sent.';
      pushFeed(`${action} ${mode} sent to ${state.selectedDeviceId}`);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        result.textContent = `Failed: ${err.message}`;
      }
    }
  }

  async function quickBuzz() {
    const result = byId('hp2-action-result');
    if (!state.selectedDeviceId) {
      result.textContent = 'Select a device first.';
      return;
    }

    const payload = {
      device_id: state.selectedDeviceId,
      action: 'LOVENSE_COMMAND',
      payload: {
        command: 'vibrate',
        intensity: 8,
        length: 700,
        level: 8,
        duration_ms: 700,
      },
    };

    result.textContent = 'Sending quick buzz...';
    try {
      await apiPost('/api/handler/tpe/push', payload);
      result.textContent = 'Quick buzz sent.';
      pushFeed(`Quick buzz sent to ${state.selectedDeviceId}`);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        result.textContent = `Failed: ${err.message}`;
      }
    }
  }

  function handleDeviceListClick(event) {
    const btn = event.target.closest('button[data-device-id]');
    if (!btn) return;
    state.selectedDeviceId = btn.dataset.deviceId;
    renderDeviceList();
    renderSelectedDevice();
    refreshSelectedStatus().catch(() => {});
    loadDashboardIntelligence().catch(() => {});
    pushFeed(`Selected ${state.selectedDeviceId}`);
  }

  function bindEvents() {
    byId('hp2-login-btn').addEventListener('click', () => {
      login().catch(() => {});
    });
    ['hp2-user', 'hp2-pass'].forEach((id) => {
      byId(id).addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          login().catch(() => {});
        }
      });
    });

    byId('hp2-logout-btn').addEventListener('click', () => showLogin(''));
    byId('hp2-lock-btn').addEventListener('click', () => lockSelected().catch(() => {}));
    byId('hp2-checkin-btn').addEventListener('click', () => requestCheckin().catch(() => {}));
    byId('hp2-send-toy-btn').addEventListener('click', () => sendToyCommand().catch(() => {}));
    byId('hp2-buzz-btn').addEventListener('click', () => quickBuzz().catch(() => {}));
    byId('hp2-refresh-btn').addEventListener('click', () => hydrateApp().catch(() => {}));
    byId('hp2-autofollow-btn').addEventListener('click', toggleAutoFollow);
    byId('hp2-refresh-intel-btn').addEventListener('click', () => loadDashboardIntelligence().catch(() => {}));
    byId('hp2-hard-refresh-btn').addEventListener('click', () => hydrateApp().catch(() => {}));

    byId('hp2-device-list').addEventListener('click', handleDeviceListClick);

    document.querySelectorAll('.hp2-tab-btn').forEach((btn) => {
      btn.addEventListener('click', () => setActiveView(btn.dataset.view));
    });
  }

  async function boot() {
    bindEvents();
    renderAutoFollowButton();
    const jwt = getJwt();
    if (!jwt) {
      showLogin('');
      return;
    }

    try {
      const me = await apiGet('/api/handler/status');
      state.role = me?.role || state.role || 'handler';
      byId('hp2-role').textContent = String(state.role || 'handler').toUpperCase();
      showApp();
      await hydrateApp();
    } catch (err) {
      if (err && err.message === AUTH_EXPIRED_ERROR) {
        showLogin('Please login again.');
        return;
      }

      // Keep a valid JWT session active even if non-critical bootstrap calls fail.
      showApp();
      byId('hp2-role').textContent = String(state.role || 'handler').toUpperCase();
      pushFeed('Restored session with limited data.');
      await hydrateApp();
    }
  }

  boot().catch(() => showLogin('Unable to initialize panel.'));
})();
