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
    connectWs();
    pushFeed('Session ready.');
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
