(() => {
  'use strict';

  const JWT_KEY = 'handler_panel_jwt';
  const AUTH_EXPIRED_ERROR = 'auth-expired';
  const views = ['dashboard', 'devices', 'commands', 'queues', 'settings'];

  const state = {
    role: null,
    selectedDeviceId: null,
    ws: null,
    wsReconnectTimer: null,
    devices: {},
    feed: [],
    intelligence: {
      alerts: [],
      transport: {
        mqtt: 0,
        wsFallback: 0,
        failures: 0,
        recent: [],
      },
      staleRisk: [],
    },
  };

  function byId(id) {
    return document.getElementById(id);
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
    pill.textContent = online ? 'WS ONLINE' : 'WS OFFLINE';
    pill.classList.toggle('hp2-pill-online', online);
    pill.classList.toggle('hp2-pill-offline', !online);
  }

  function showLogin(message = '') {
    disconnectWs();
    byId('hp2-app').hidden = true;
    byId('hp2-login').hidden = false;
    byId('hp2-login-error').textContent = message;
    clearJwt();
  }

  function showApp() {
    byId('hp2-login').hidden = true;
    byId('hp2-app').hidden = false;
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
    document.querySelectorAll('.hp2-nav-btn').forEach((button) => {
      button.classList.toggle('hp2-nav-active', button.dataset.view === viewName);
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
    await loadDevices();
    await loadQueueKpis();
    await loadDashboardIntelligence();
    connectWs();
    pushFeed('Session ready.');
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
  }

  async function loadDashboardIntelligence() {
    const [events, audits] = await Promise.all([
      apiGet('/api/handler/tpe/events?limit=120').catch(() => []),
      apiGet('/api/handler/tpe/audits?limit=80').catch(() => []),
    ]);

    deriveTransportOutcomes(events);
    deriveAlertsTimeline(events, audits);
    deriveStaleRisk();
    renderDashboardIntelligence();
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

  function handleDeviceListClick(event) {
    const btn = event.target.closest('button[data-device-id]');
    if (!btn) return;
    state.selectedDeviceId = btn.dataset.deviceId;
    renderDeviceList();
    renderSelectedDevice();
    refreshSelectedStatus().catch(() => {});
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
    byId('hp2-refresh-btn').addEventListener('click', () => hydrateApp().catch(() => {}));
    byId('hp2-refresh-intel-btn').addEventListener('click', () => loadDashboardIntelligence().catch(() => {}));
    byId('hp2-hard-refresh-btn').addEventListener('click', () => hydrateApp().catch(() => {}));

    byId('hp2-device-list').addEventListener('click', handleDeviceListClick);

    document.querySelectorAll('.hp2-nav-btn').forEach((btn) => {
      btn.addEventListener('click', () => setActiveView(btn.dataset.view));
    });
  }

  async function boot() {
    bindEvents();
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
    } catch (_e) {
      showLogin('Please login again.');
    }
  }

  boot().catch(() => showLogin('Unable to initialize panel.'));
})();
