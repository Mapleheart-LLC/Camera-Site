(() => {
  'use strict';

  const JWT_KEY = 'handler_panel_jwt';
  const SETTINGS_KEY = 'handler_panel_v2_settings';
  const AUTH_EXPIRED_ERROR = 'auth-expired';
  const QUEUE_AUTO_REFRESH_MS = 30000;
  const views = ['dashboard', 'stats', 'queue', 'drawer', 'devices', 'commands', 'settings'];
  const MAX_BREADCRUMBS = 6;
  const defaultSettings = {
    refreshSecs: 30,
    freshnessWarnSecs: 60,
    freshnessStaleSecs: 180,
    defaultIntensity: 10,
    defaultLengthMs: 800,
  };

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
    queue: {
      selectedMailThreadId: null,
      mailMessagesById: {},
      mailThreadIds: [],
      mailThreadsById: {},
      mailSeenAtByThread: {},
      pendingMessagesByThread: {},
      selectedMailMessages: [],
      autoRefreshTimer: null,
      openQuestions: [],
      answeredQuestions: [],
      openVisible: 3,
      answeredVisible: 3,
      sharedFilter: 'all',
      sharedSort: 'newest',
    },
    telemetry: {
      hydratedAt: 0,
      devicesAt: 0,
      intelligenceAt: 0,
      queueAt: 0,
      drawerAt: 0,
      freshnessTimer: null,
    },
    settings: { ...defaultSettings },
    commands: {
      history: [],
      smsThreadPresets: ['default'],
      liveControl: {
        quickTapTarget: 'lovense',
        quickTapAction: 'vibrate',
      },
    },
    modal: {
      resolver: null,
      requireInput: false,
      allowEmpty: false,
      multiline: false,
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

  function getQueueRefreshMs() {
    const secs = Math.max(30, Number(state.settings.refreshSecs || defaultSettings.refreshSecs));
    return secs * 1000;
  }

  function loadSettings() {
    let parsed = {};
    try {
      parsed = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}') || {};
    } catch (_err) {
      parsed = {};
    }

    state.settings = {
      refreshSecs: Math.max(30, Number(parsed.refreshSecs || defaultSettings.refreshSecs)),
      freshnessWarnSecs: Math.max(30, Number(parsed.freshnessWarnSecs || defaultSettings.freshnessWarnSecs)),
      freshnessStaleSecs: Math.max(60, Number(parsed.freshnessStaleSecs || defaultSettings.freshnessStaleSecs)),
      defaultIntensity: Math.min(20, Math.max(1, Number(parsed.defaultIntensity || defaultSettings.defaultIntensity))),
      defaultLengthMs: Math.min(20000, Math.max(100, Number(parsed.defaultLengthMs || defaultSettings.defaultLengthMs))),
    };
    if (state.settings.freshnessStaleSecs <= state.settings.freshnessWarnSecs) {
      state.settings.freshnessStaleSecs = state.settings.freshnessWarnSecs + 30;
    }
  }

  function saveSettings() {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(state.settings));
  }

  function renderSettingsForm() {
    const map = {
      'hp2-setting-refresh-secs': state.settings.refreshSecs,
      'hp2-setting-fresh-warn': state.settings.freshnessWarnSecs,
      'hp2-setting-fresh-stale': state.settings.freshnessStaleSecs,
      'hp2-setting-default-intensity': state.settings.defaultIntensity,
      'hp2-setting-default-length': state.settings.defaultLengthMs,
    };
    Object.entries(map).forEach(([id, value]) => {
      const el = byId(id);
      if (el) el.value = String(value);
    });
  }

  function applyCommandDefaults() {
    const quickTapIntensity = byId('hp2-quicktap-intensity');
    const quickTapLength = byId('hp2-quicktap-length');
    const lovenseLiveLevel = byId('hp2-lovense-live-level');

    if (quickTapIntensity) quickTapIntensity.value = String(state.settings.defaultIntensity);
    if (quickTapLength) quickTapLength.value = String(state.settings.defaultLengthMs);
    if (lovenseLiveLevel) lovenseLiveLevel.value = String(state.settings.defaultIntensity);
    syncControlReadouts();
  }

  function saveSettingsFromForm() {
    const refreshSecs = Number(byId('hp2-setting-refresh-secs')?.value || state.settings.refreshSecs);
    const warnSecs = Number(byId('hp2-setting-fresh-warn')?.value || state.settings.freshnessWarnSecs);
    const staleSecs = Number(byId('hp2-setting-fresh-stale')?.value || state.settings.freshnessStaleSecs);
    const defaultIntensity = Number(byId('hp2-setting-default-intensity')?.value || state.settings.defaultIntensity);
    const defaultLengthMs = Number(byId('hp2-setting-default-length')?.value || state.settings.defaultLengthMs);

    state.settings.refreshSecs = Math.max(30, Math.min(300, refreshSecs || defaultSettings.refreshSecs));
    state.settings.freshnessWarnSecs = Math.max(30, Math.min(600, warnSecs || defaultSettings.freshnessWarnSecs));
    state.settings.freshnessStaleSecs = Math.max(
      state.settings.freshnessWarnSecs + 30,
      Math.min(1200, staleSecs || defaultSettings.freshnessStaleSecs),
    );
    state.settings.defaultIntensity = Math.max(1, Math.min(20, defaultIntensity || defaultSettings.defaultIntensity));
    state.settings.defaultLengthMs = Math.max(100, Math.min(20000, defaultLengthMs || defaultSettings.defaultLengthMs));

    saveSettings();
    renderSettingsForm();
    applyCommandDefaults();
    renderFreshness();

    if (byId('hp2-view-queue')?.classList.contains('hp2-view-active')) {
      setActiveView('queue');
    }
    if (byId('hp2-view-drawer')?.classList.contains('hp2-view-active')) {
      setActiveView('drawer');
    }
  }

  function renderCommandHistory() {
    const host = byId('hp2-command-history');
    if (!host) return;
    if (!state.commands.history.length) {
      host.innerHTML = '<li class="hp2-muted">No command dispatches yet.</li>';
      return;
    }
    host.innerHTML = state.commands.history.map((row) => `<li>
      <div class="hp2-feed-item-row">
        <strong>${escapeHtml(row.title)}</strong>
        <span class="${severityClass(row.ok ? 'info' : 'critical')}">${row.ok ? 'ok' : 'failed'}</span>
      </div>
      <div class="hp2-muted">${escapeHtml(row.detail)}</div>
      <div class="hp2-muted">${escapeHtml(fmtDate(row.at))}</div>
    </li>`).join('');
  }

  function recordCommandHistory(title, detail, ok) {
    state.commands.history.unshift({
      title,
      detail,
      ok: !!ok,
      at: new Date().toISOString(),
    });
    state.commands.history = state.commands.history.slice(0, 12);
    renderCommandHistory();
  }

  function quickTapActionsForTarget(target) {
    if (target === 'pavlok') {
      return ['shock', 'vibrate', 'beep', 'stop'];
    }
    return ['vibrate', 'pulse', 'wave', 'tease', 'stop'];
  }

  function setSegmentActive(host, value) {
    if (!host) return;
    host.querySelectorAll('[data-segment-value]').forEach((button) => {
      const active = button.dataset.segmentValue === value;
      button.classList.toggle('hp2-segment-active', active);
    });
  }

  function syncControlReadouts() {
    const pairs = [
      ['hp2-quicktap-intensity', 'hp2-quicktap-intensity-value'],
      ['hp2-lovense-live-level', 'hp2-lovense-live-level-value'],
      ['hp2-lovense-ramp-min', 'hp2-lovense-ramp-min-value'],
      ['hp2-lovense-ramp-max', 'hp2-lovense-ramp-max-value'],
      ['hp2-pavlok-intensity', 'hp2-pavlok-intensity-value'],
      ['hp2-screenctl-brightness', 'hp2-screenctl-brightness-value'],
    ];
    pairs.forEach(([inputId, outputId]) => {
      const input = byId(inputId);
      const output = byId(outputId);
      if (!input || !output) return;
      output.textContent = String(input.value || '0');
    });

    const quickTapLengthWrap = byId('hp2-quicktap-length-wrap');
    const quickTapNote = byId('hp2-quicktap-note');
    if (quickTapLengthWrap) {
      quickTapLengthWrap.style.opacity = (state.commands.liveControl.quickTapTarget === 'pavlok' && state.commands.liveControl.quickTapAction === 'shock') ? '0.55' : '1';
    }
    if (quickTapNote) {
      quickTapNote.textContent = (state.commands.liveControl.quickTapTarget === 'pavlok' && state.commands.liveControl.quickTapAction === 'shock')
        ? 'Pavlok shock ignores length and uses intensity-first dispatch.'
        : 'Length stays available for timed taps. Pavlok shock ignores length.';
    }

    const pavlokCmd = String(byId('hp2-pavlok-command')?.value || 'shock').toLowerCase();
    const pavlokDurationWrap = byId('hp2-pavlok-duration-wrap');
    const pavlokNote = byId('hp2-pavlok-note');
    const hidePavlokDuration = pavlokCmd === 'shock' || pavlokCmd === 'stop';
    if (pavlokDurationWrap) {
      pavlokDurationWrap.style.opacity = hidePavlokDuration ? '0.55' : '1';
    }
    if (pavlokNote) {
      pavlokNote.textContent = hidePavlokDuration
        ? 'Current Pavlok command is intensity-first; timed length is ignored here.'
        : 'Timed length is active for vibrate and beep commands.';
    }

    applyCommandGating();
  }

  function renderQuickTapActionButtons() {
    const host = byId('hp2-quicktap-actions');
    if (!host) return;
    const target = state.commands.liveControl.quickTapTarget;
    const allowed = quickTapActionsForTarget(target);
    if (!allowed.includes(state.commands.liveControl.quickTapAction)) {
      [state.commands.liveControl.quickTapAction] = allowed;
    }
    host.classList.toggle('hp2-segmented-row-3', allowed.length >= 3);
    host.innerHTML = allowed.map((action) => `<button type="button" class="hp2-btn hp2-btn-ghost" data-segment-value="${escapeHtml(action)}">${escapeHtml(action)}</button>`).join('');
    host.querySelectorAll('[data-segment-value]').forEach((button) => {
      button.addEventListener('click', () => {
        state.commands.liveControl.quickTapAction = button.dataset.segmentValue || 'vibrate';
        setSegmentActive(host, state.commands.liveControl.quickTapAction);
        syncControlReadouts();
      });
    });
    setSegmentActive(host, state.commands.liveControl.quickTapAction);
    syncControlReadouts();
  }

  function renderSmsThreadPresetList() {
    const list = byId('hp2-sms-thread-list');
    if (!list) return;
    const values = Array.isArray(state.commands.smsThreadPresets) ? state.commands.smsThreadPresets : ['default'];
    const uniq = Array.from(new Set(values.map((value) => String(value || '').trim()).filter(Boolean)));
    if (!uniq.includes('default')) uniq.unshift('default');
    list.innerHTML = uniq.map((threadId) => `<option value="${escapeHtml(threadId)}"></option>`).join('');
    state.commands.smsThreadPresets = uniq;
  }

  async function loadSmsThreadPresets() {
    const presetSet = new Set(['default']);

    const [smsThreads, puppyThreads] = await Promise.all([
      apiGet('/api/admin/sms/threads').catch(() => []),
      apiGet('/api/handler/puppy-mail/threads?status=all&limit=200').catch(() => []),
    ]);

    if (Array.isArray(smsThreads)) {
      smsThreads.forEach((row) => {
        const id = String(row?.thread_id || '').trim();
        if (id) presetSet.add(id);
      });
    }

    if (Array.isArray(puppyThreads)) {
      puppyThreads.forEach((row) => {
        const id = String(row?.id || '').trim();
        if (id) presetSet.add(id);
      });
    }

    state.commands.smsThreadPresets = Array.from(presetSet);
    renderSmsThreadPresetList();
  }

  function setQuickTapTarget(target) {
    state.commands.liveControl.quickTapTarget = target === 'pavlok' ? 'pavlok' : 'lovense';
    setSegmentActive(byId('hp2-quicktap-target-lovense')?.parentElement || null, state.commands.liveControl.quickTapTarget);
    renderQuickTapActionButtons();
    applyCommandGating();
  }

  function applyCommandPreset(presetName) {
    const presets = {
      calm: { target: 'lovense', action: 'pulse', intensity: 5, length: 500 },
      steady: { target: 'lovense', action: 'vibrate', intensity: 9, length: 1000 },
      alert: { target: 'pavlok', action: 'vibrate', intensity: 90, length: 1400 },
    };
    const preset = presets[presetName];
    if (!preset) return;
    setQuickTapTarget(preset.target);
    state.commands.liveControl.quickTapAction = preset.action;
    const intensityEl = byId('hp2-quicktap-intensity');
    const lengthEl = byId('hp2-quicktap-length');
    if (intensityEl) intensityEl.value = String(preset.intensity);
    if (lengthEl) lengthEl.value = String(preset.length);
    renderQuickTapActionButtons();
    setInlineResult('hp2-quicktap-result', `Preset loaded: ${presetName}.`);
  }

  function parseLovenseSchedules(raw) {
    return String(raw || '')
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        const parts = line.split(',').map((part) => part.trim());
        const [time = '', level = '10', duration = '5000', label = ''] = parts;
        return {
          at: time,
          level: Number(level || 10),
          duration_ms: Number(duration || 5000),
          label,
        };
      })
      .filter((row) => row.at);
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

  async function apiPatch(path, body) {
    const response = await apiFetch(path, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    return response.json().catch(() => ({}));
  }

  function selectedDevice() {
    return state.selectedDeviceId ? state.devices[state.selectedDeviceId] : null;
  }

  function selectedDeviceLabel() {
    const device = selectedDevice();
    return device?.device_name || device?.device_id || state.selectedDeviceId || 'selected device';
  }

  function parseToyInfo(device) {
    if (!device) return {};
    if (device.toy_info && typeof device.toy_info === 'object') return device.toy_info;
    if (device.toy_info_json && typeof device.toy_info_json === 'string') {
      try {
        const parsed = JSON.parse(device.toy_info_json);
        if (parsed && typeof parsed === 'object') return parsed;
      } catch (_err) {
        return {};
      }
    }
    return {};
  }

  function hasToyCapability(device, mode) {
    const info = parseToyInfo(device);
    const text = JSON.stringify(info || {}).toLowerCase();
    const token = String(mode || '').toLowerCase();
    if (!token) return false;
    if (text.includes(token)) return true;
    if (token === 'lovense' && (text.includes('nora') || text.includes('lush') || text.includes('hush'))) return true;
    if (token === 'pavlok' && (text.includes('zap') || text.includes('shock'))) return true;
    return false;
  }

  function commandCapabilities(device) {
    const online = deviceOnline(device);
    const lovense = hasToyCapability(device, 'lovense');
    const pavlok = hasToyCapability(device, 'pavlok');
    const toyInfoKnown = JSON.stringify(parseToyInfo(device)).length > 2;
    return {
      online,
      lovense,
      pavlok,
      toyInfoKnown,
      selected: !!device,
    };
  }

  function setButtonEnabled(id, enabled, reason) {
    const el = byId(id);
    if (!el) return;
    el.disabled = !enabled;
    if (!enabled && reason) {
      el.title = reason;
    } else {
      el.title = '';
    }
  }

  function setDisabledHint(id, message) {
    const el = byId(id);
    if (!el) return;
    const text = String(message || '').trim();
    el.textContent = text;
    el.classList.toggle('hp2-hidden', !text);
  }

  function renderCommandReadiness() {
    const chipsHost = byId('hp2-command-capability-chips');
    const noteEl = byId('hp2-command-capability-note');
    if (!chipsHost || !noteEl) return;

    const device = selectedDevice();
    const caps = commandCapabilities(device);

    if (!caps.selected) {
      chipsHost.innerHTML = '<span class="hp2-capability-chip hp2-capability-off">No Device</span>';
      noteEl.textContent = 'Select a device to evaluate command readiness.';
      return;
    }

    const rows = [
      { label: 'Online', on: caps.online },
      { label: 'Lovense', on: caps.lovense },
      { label: 'Pavlok', on: caps.pavlok },
      { label: 'Toy Info', on: caps.toyInfoKnown },
    ];
    chipsHost.innerHTML = rows
      .map((row) => `<span class="hp2-capability-chip ${row.on ? 'hp2-capability-on' : 'hp2-capability-off'}">${escapeHtml(row.label)} ${row.on ? 'Ready' : 'Missing'}</span>`)
      .join('');

    if (!caps.online) {
      noteEl.textContent = 'Device appears offline. Transport can fail until it reconnects.';
    } else if (!caps.toyInfoKnown) {
      noteEl.textContent = 'Toy capability is unknown. Toy controls remain limited until heartbeat reports toy info.';
    } else {
      noteEl.textContent = 'Device is online. Command gating is active for toy-specific actions.';
    }
  }

  function applyCommandGating() {
    const device = selectedDevice();
    const caps = commandCapabilities(device);
    const noDeviceReason = 'Select a device first.';
    const onlineReason = 'Device appears offline.';
    const lovenseReason = 'Lovense capability not detected from toy heartbeat.';
    const pavlokReason = 'Pavlok capability not detected from toy heartbeat.';
    const onlineGateReason = !caps.selected ? noDeviceReason : onlineReason;

    const needsOnline = [
      'hp2-appctl-send-btn',
      'hp2-screenctl-lock-btn',
      'hp2-screenctl-dismiss-keyguard-btn',
      'hp2-screenctl-on-btn',
      'hp2-screenctl-off-btn',
      'hp2-screenctl-brightness-send-btn',
      'hp2-screenctl-timeout-send-btn',
      'hp2-screenctl-autorotate-send-btn',
      'hp2-screenctl-url-send-btn',
      'hp2-notify-send-btn',
      'hp2-notify-clear-btn',
      'hp2-speak-send-btn',
      'hp2-clipboard-send-btn',
      'hp2-sms-inject-btn',
      'hp2-sms-reply-toggle-btn',
    ];
    needsOnline.forEach((id) => setButtonEnabled(id, caps.selected && caps.online, onlineGateReason));

    const lovenseEnabled = caps.selected && caps.online && caps.lovense;
    const pavlokEnabled = caps.selected && caps.online && caps.pavlok;
    const lovenseHint = !caps.selected ? noDeviceReason : (!caps.online ? onlineReason : lovenseReason);
    const pavlokHint = !caps.selected ? noDeviceReason : (!caps.online ? onlineReason : pavlokReason);

    setButtonEnabled('hp2-lovense-live-start-btn', lovenseEnabled, lovenseHint);
    setButtonEnabled('hp2-lovense-ramp-start-btn', lovenseEnabled, lovenseHint);
    setButtonEnabled('hp2-lovense-schedule-send-btn', lovenseEnabled, lovenseHint);
    setButtonEnabled('hp2-startle-btn', lovenseEnabled, lovenseHint);
    setButtonEnabled('hp2-pavlok-send-btn', pavlokEnabled, pavlokHint);
    ['hp2-shock-10-btn', 'hp2-shock-30-btn', 'hp2-shock-60-btn'].forEach((id) => {
      setButtonEnabled(id, pavlokEnabled, pavlokHint);
    });

    const quickTapTarget = state.commands.liveControl.quickTapTarget;
    const quickTapEnabled = quickTapTarget === 'pavlok' ? pavlokEnabled : lovenseEnabled;
    const quickTapHint = quickTapTarget === 'pavlok' ? pavlokHint : lovenseHint;
    setButtonEnabled('hp2-quicktap-send-btn', quickTapEnabled, quickTapHint);

    setDisabledHint('hp2-quicktap-disabled-hint', quickTapEnabled ? '' : quickTapHint);
    setDisabledHint('hp2-lovense-disabled-hint', lovenseEnabled ? '' : lovenseHint);
    setDisabledHint('hp2-pavlok-disabled-hint', pavlokEnabled ? '' : pavlokHint);
    setDisabledHint('hp2-appctl-disabled-hint', caps.selected && caps.online ? '' : onlineGateReason);
    setDisabledHint('hp2-screenctl-disabled-hint', caps.selected && caps.online ? '' : onlineGateReason);
    setDisabledHint('hp2-notify-disabled-hint', caps.selected && caps.online ? '' : onlineGateReason);
    setDisabledHint('hp2-clipboard-disabled-hint', caps.selected && caps.online ? '' : onlineGateReason);

    renderCommandReadiness();
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
    if (state.telemetry.freshnessTimer) {
      clearInterval(state.telemetry.freshnessTimer);
      state.telemetry.freshnessTimer = null;
    }
    document.body.classList.remove('hp2-authenticated');
    setVisible('hp2-app', false);
    setVisible('hp2-login', true);
    byId('hp2-login-error').textContent = message;
    clearJwt();
  }

  function showApp() {
    document.body.classList.add('hp2-authenticated');
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

  function parseTimeValue(iso) {
    const value = new Date(iso || '').getTime();
    return Number.isFinite(value) ? value : 0;
  }

  function fmtChatTime(iso) {
    const d = new Date(iso || '');
    if (Number.isNaN(d.getTime())) return '-';
    return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  }

  function fmtChatDayLabel(iso) {
    const d = new Date(iso || '');
    if (Number.isNaN(d.getTime())) return 'Unknown day';
    const day = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const diffDays = Math.round((today.getTime() - day.getTime()) / 86400000);
    if (diffDays === 0) return 'Today';
    if (diffDays === 1) return 'Yesterday';
    return day.toLocaleDateString();
  }

  function sharedSortSign() {
    return state.queue.sharedSort === 'oldest' ? 1 : -1;
  }

  function compareByTimeDescOrAsc(aIso, bIso) {
    const sign = sharedSortSign();
    return (parseTimeValue(aIso) - parseTimeValue(bIso)) * sign;
  }

  function isResolvedStatus(status) {
    const s = String(status || '').toLowerCase();
    return s === 'resolved' || s === 'done' || s === 'answered' || s === 'dismissed';
  }

  function includeBySharedFilter(status, mode = null) {
    const shared = state.queue.sharedFilter;
    if (shared === 'all') return true;
    if (shared === 'open') {
      if (mode === 'open-only') return true;
      if (mode === 'resolved-only') return false;
      return !isResolvedStatus(status);
    }
    if (shared === 'resolved') {
      if (mode === 'open-only') return false;
      if (mode === 'resolved-only') return true;
      return isResolvedStatus(status);
    }
    return true;
  }

  function syncSharedControls() {
    const fQueue = byId('hp2-shared-filter-queue');
    const fDrawer = byId('hp2-shared-filter-drawer');
    const sQueue = byId('hp2-shared-sort-queue');
    const sDrawer = byId('hp2-shared-sort-drawer');
    [fQueue, fDrawer].forEach((el) => {
      if (el) el.value = state.queue.sharedFilter;
    });
    [sQueue, sDrawer].forEach((el) => {
      if (el) el.value = state.queue.sharedSort;
    });
  }

  function updateSharedControls({ filter, sort }) {
    if (filter) state.queue.sharedFilter = filter;
    if (sort) state.queue.sharedSort = sort;
    syncSharedControls();

    const queueActive = byId('hp2-view-queue')?.classList.contains('hp2-view-active');
    const drawerActive = byId('hp2-view-drawer')?.classList.contains('hp2-view-active');
    if (queueActive) loadQueueHub().catch(() => {});
    if (drawerActive) loadEvidenceDrawer().catch(() => {});
  }

  function showToast(message, tone = 'info') {
    const host = byId('hp2-toast-stack');
    if (!host || !message) return;
    const toast = document.createElement('div');
    toast.className = `hp2-toast ${tone === 'ok' ? 'hp2-toast-ok' : tone === 'warn' ? 'hp2-toast-warn' : tone === 'bad' ? 'hp2-toast-bad' : ''}`.trim();
    toast.textContent = message;
    host.prepend(toast);
    while (host.children.length > 4) {
      host.removeChild(host.lastChild);
    }
    setTimeout(() => {
      if (toast.parentElement === host) {
        host.removeChild(toast);
      }
    }, 3600);
  }

  function setInlineResult(id, message) {
    const el = byId(id);
    if (el) el.textContent = message || '';
    if (!message) return;
    if (String(message).endsWith('...')) return;
    const lower = String(message).toLowerCase();
    const tone = lower.includes('failed') || lower.includes('error') ? 'bad' : (lower.includes('warning') ? 'warn' : 'ok');
    showToast(message, tone);
  }

  function renderFreshness() {
    const pill = byId('hp2-freshness-pill');
    const note = byId('hp2-freshness-note');
    if (!pill || !note) return;

    const now = Date.now();
    const ages = [state.telemetry.devicesAt, state.telemetry.intelligenceAt].filter((v) => Number(v) > 0).map((v) => Math.max(0, Math.round((now - v) / 1000)));
    if (!ages.length) {
      pill.textContent = 'Data age: -';
      note.textContent = 'Waiting for telemetry...';
      return;
    }

    const maxAge = Math.max(...ages);
    const warnAt = Math.max(30, Number(state.settings.freshnessWarnSecs || defaultSettings.freshnessWarnSecs));
    const staleAt = Math.max(warnAt + 1, Number(state.settings.freshnessStaleSecs || defaultSettings.freshnessStaleSecs));
    let tone = 'fresh';
    if (maxAge >= staleAt) tone = 'stale';
    else if (maxAge >= warnAt) tone = 'aging';

    pill.textContent = `Data age: ${maxAge}s`;
    pill.classList.toggle('hp2-pill-online', tone === 'fresh');
    pill.classList.toggle('hp2-pill-offline', tone === 'stale');
    note.textContent = tone === 'fresh'
      ? 'Telemetry is current.'
      : (tone === 'aging' ? 'Telemetry is aging. Consider refresh.' : 'Telemetry is stale. Refresh recommended.');
  }

  function markThreadSeen(threadId, iso) {
    const id = Number(threadId || 0);
    if (!id) return;
    const ts = parseTimeValue(iso) || Date.now();
    const prev = Number(state.queue.mailSeenAtByThread[id] || 0);
    state.queue.mailSeenAtByThread[id] = Math.max(prev, ts);
  }

  function getPendingMessages(threadId) {
    const id = Number(threadId || 0);
    return Array.isArray(state.queue.pendingMessagesByThread[id]) ? state.queue.pendingMessagesByThread[id] : [];
  }

  function setPendingMessages(threadId, rows) {
    const id = Number(threadId || 0);
    if (!id) return;
    state.queue.pendingMessagesByThread[id] = Array.isArray(rows) ? rows : [];
  }

  function addPendingMessage(threadId, body) {
    const id = Number(threadId || 0);
    if (!id) return '';
    const tempId = `tmp-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const rows = getPendingMessages(id);
    rows.push({
      tempId,
      body,
      author: 'Handler',
      created_at: new Date().toISOString(),
      delivery: 'sending',
      error: '',
    });
    setPendingMessages(id, rows);
    return tempId;
  }

  function patchPendingMessage(threadId, tempId, patch) {
    const rows = getPendingMessages(threadId);
    const next = rows.map((row) => (row.tempId === tempId ? { ...row, ...(patch || {}) } : row));
    setPendingMessages(threadId, next);
  }

  function removePendingMessage(threadId, tempId) {
    const rows = getPendingMessages(threadId).filter((row) => row.tempId !== tempId);
    setPendingMessages(threadId, rows);
  }

  function selectAdjacentMailThread(direction) {
    if (!byId('hp2-view-queue').classList.contains('hp2-view-active')) return;
    const ids = state.queue.mailThreadIds;
    if (!ids.length) return;
    const current = Number(state.queue.selectedMailThreadId || 0);
    const at = ids.indexOf(current);
    const base = at >= 0 ? at : 0;
    const target = ids[Math.max(0, Math.min(ids.length - 1, base + direction))];
    if (!target || target === current) return;
    loadQueueMailThread(target).catch(() => {});
  }

  function setActiveView(viewName) {
    if (state.queue.autoRefreshTimer) {
      clearInterval(state.queue.autoRefreshTimer);
      state.queue.autoRefreshTimer = null;
    }

    views.forEach((view) => {
      byId(`hp2-view-${view}`).classList.toggle('hp2-view-active', view === viewName);
    });
    document.querySelectorAll('.hp2-tab-btn').forEach((button) => {
      button.classList.toggle('hp2-tab-active', button.dataset.view === viewName);
    });
    if (viewName === 'queue') {
      loadQueueHub().catch(() => {});
      state.queue.autoRefreshTimer = setInterval(() => {
        loadQueueHub().catch(() => {});
      }, getQueueRefreshMs());
    }
    if (viewName === 'drawer') {
      loadEvidenceDrawer().catch(() => {});
      state.queue.autoRefreshTimer = setInterval(() => {
        loadEvidenceDrawer().catch(() => {});
      }, getQueueRefreshMs());
    }
  }

  function renderKpis() {
    const devices = Object.values(state.devices);
    const online = devices.filter((d) => deviceOnline(d)).length;
    const locked = devices.filter((d) => Number(d.is_locked || 0) === 1).length;
    const batteryRows = devices.map((d) => Number(d.battery_pct)).filter((v) => Number.isFinite(v));
    const avgBattery = batteryRows.length
      ? `${Math.round(batteryRows.reduce((acc, v) => acc + v, 0) / batteryRows.length)}%`
      : '-';

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
    const d = selectedDevice();
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
    applyCommandGating();
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
    window.L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', {
      maxZoom: 19,
      subdomains: 'abcd',
      attribution: '&copy; OpenStreetMap contributors &copy; CARTO',
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
    state.telemetry.devicesAt = Date.now();
    renderFreshness();
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
        state.telemetry.devicesAt = Date.now();
        renderFreshness();
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
        state.telemetry.devicesAt = Date.now();
        renderFreshness();
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
      button.textContent = 'Enter Panel';
    }
  }

  async function hydrateApp() {
    const jobs = [
      loadDevices(),
      loadDashboardIntelligence(),
      loadQueueHub(),
      loadSmsThreadPresets(),
    ];
    const results = await Promise.allSettled(jobs);
    state.telemetry.hydratedAt = Date.now();
    connectWs();
    pushFeed('Session ready.');

    const failed = results.filter((r) => r.status === 'rejected');
    if (failed.length) {
      pushFeed(`Loaded with ${failed.length} warning(s).`);
      setInlineResult('hp2-action-result', 'Signed in with partial data. Use refresh if a panel is empty.');
    }
    renderFreshness();
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
    state.telemetry.intelligenceAt = Date.now();
    renderFreshness();
  }

  function setQueueResult(message) {
    setInlineResult('hp2-queue-result', message || '');
  }

  function isInlineModalOpen() {
    const modal = byId('hp2-modal');
    return !!modal && !modal.classList.contains('hp2-hidden');
  }

  function closeInlineModal(result) {
    const modal = byId('hp2-modal');
    if (!modal) return;
    modal.classList.add('hp2-hidden');
    document.body.classList.remove('hp2-modal-open');
    const resolver = state.modal.resolver;
    state.modal.resolver = null;
    state.modal.requireInput = false;
    state.modal.allowEmpty = false;
    state.modal.multiline = false;
    if (resolver) resolver(result || { confirmed: false, value: '' });
  }

  function showInlineModal(options = {}) {
    const modal = byId('hp2-modal');
    const titleEl = byId('hp2-modal-title');
    const msgEl = byId('hp2-modal-message');
    const fieldWrap = byId('hp2-modal-input-wrap');
    const fieldLabel = byId('hp2-modal-input-label');
    const field = byId('hp2-modal-input');
    const cancelBtn = byId('hp2-modal-cancel-btn');
    const confirmBtn = byId('hp2-modal-confirm-btn');

    if (!modal || !titleEl || !msgEl || !fieldWrap || !fieldLabel || !field || !cancelBtn || !confirmBtn) {
      return Promise.resolve({ confirmed: false, value: '' });
    }

    const wantsInput = !!options.withInput;
    state.modal.requireInput = wantsInput;
    state.modal.allowEmpty = !!options.allowEmpty;
    state.modal.multiline = !!options.multiline;

    titleEl.textContent = options.title || 'Confirm action';
    msgEl.textContent = options.message || '';
    fieldWrap.classList.toggle('hp2-hidden', !wantsInput);
    fieldLabel.textContent = options.inputLabel || 'Value';
    field.value = options.inputValue || '';
    field.placeholder = options.inputPlaceholder || '';
    field.rows = state.modal.multiline ? 4 : 1;

    cancelBtn.textContent = options.cancelText || 'Cancel';
    cancelBtn.classList.toggle('hp2-hidden', !!options.confirmOnly);
    confirmBtn.textContent = options.confirmText || 'Confirm';
    confirmBtn.classList.toggle('hp2-btn-warn', !!options.danger);
    confirmBtn.classList.toggle('hp2-btn-primary', !options.danger);

    modal.classList.remove('hp2-hidden');
    document.body.classList.add('hp2-modal-open');
    setTimeout(() => {
      if (wantsInput) {
        field.focus();
        field.selectionStart = field.value.length;
        field.selectionEnd = field.value.length;
      } else {
        confirmBtn.focus();
      }
    }, 0);

    return new Promise((resolve) => {
      state.modal.resolver = resolve;
    });
  }

  async function askInlineText(options = {}) {
    const result = await showInlineModal({ withInput: true, ...options });
    if (!result.confirmed) return { confirmed: false, value: '' };
    const value = String(result.value || '');
    const normalized = value.trim();
    if (!options.allowEmpty && !normalized) {
      return { confirmed: true, invalidEmpty: true, value: '' };
    }
    return { confirmed: true, value: options.allowEmpty ? value : normalized };
  }

  function askInlineConfirm(options = {}) {
    return showInlineModal({ withInput: false, ...options });
  }

  function setMailDetailsVisible(visible) {
    const details = byId('hp2-queue-mail-details');
    const toggle = byId('hp2-queue-mail-details-toggle');
    if (!details || !toggle) return;
    details.classList.toggle('hp2-hidden', !visible);
    toggle.setAttribute('aria-expanded', visible ? 'true' : 'false');
  }

  function autoSizeMailComposer() {
    const input = byId('hp2-queue-mail-reply');
    if (!input) return;
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 108)}px`;
  }

  function queueListEmpty(message) {
    return `<li class="hp2-muted">${escapeHtml(message)}</li>`;
  }

  async function loadQueueBooking() {
    const listEl = byId('hp2-queue-booking-list');
    const filter = byId('hp2-queue-booking-filter')?.value || 'new';
    listEl.innerHTML = queueListEmpty('Loading booking queue...');

    try {
      const items = await apiGet(`/api/handler/booking?status=${encodeURIComponent(filter)}&limit=200`);
      let rows = Array.isArray(items) ? items : [];
      if (filter === 'all') {
        rows = rows.filter((item) => includeBySharedFilter(item.status || 'new'));
      }
      rows.sort((a, b) => compareByTimeDescOrAsc(a.updated_at || a.created_at, b.updated_at || b.created_at));

      if (!rows.length) {
        listEl.innerHTML = queueListEmpty('No booking items in this filter.');
        return;
      }
      listEl.innerHTML = rows.map((item) => {
        const id = Number(item.id || 0);
        return `<li>
          <div class="hp2-feed-item-row">
            <strong>${escapeHtml(item.contact_handle || 'Unknown contact')}</strong>
            <span class="${severityClass(item.status === 'done' ? 'info' : 'warning')}">${escapeHtml(item.status || 'new')}</span>
          </div>
          <div class="hp2-muted">${escapeHtml(item.session_intent || 'No session intent')}</div>
          <div class="hp2-muted">${escapeHtml(item.location_text || item.availability_window || 'No location')}</div>
          <div class="hp2-queue-actions">
            <button type="button" class="hp2-btn hp2-btn-ghost" data-q-action="booking-status" data-id="${id}" data-status="qualified">Qualified</button>
            <button type="button" class="hp2-btn hp2-btn-ghost" data-q-action="booking-status" data-id="${id}" data-status="scheduled">Scheduled</button>
            <button type="button" class="hp2-btn hp2-btn-ghost" data-q-action="booking-status" data-id="${id}" data-status="done">Done</button>
          </div>
        </li>`;
      }).join('');
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        listEl.innerHTML = queueListEmpty('Failed to load booking queue.');
      }
    }
  }

  async function updateBookingStatus(id, status) {
    if (!id || !status) return;
    setQueueResult('Updating booking status...');
    try {
      await apiPost(`/api/handler/booking/${encodeURIComponent(String(id))}/status`, { status });
      setQueueResult(`Booking #${id} marked ${status}.`);
      await loadQueueBooking();
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setQueueResult(`Failed to update booking: ${err.message}`);
      }
    }
  }

  async function loadQueueMailThreads() {
    const listEl = byId('hp2-queue-mail-list');
    const filter = byId('hp2-queue-mail-filter')?.value || 'open';
    listEl.innerHTML = queueListEmpty('Loading mail threads...');

    try {
      const items = await apiGet(`/api/handler/puppy-mail/threads?status=${encodeURIComponent(filter)}&limit=200`);
      let rows = Array.isArray(items) ? items : [];
      if (filter === 'all') {
        rows = rows.filter((thread) => includeBySharedFilter(thread.status || 'open'));
      }
      rows.sort((a, b) => compareByTimeDescOrAsc(a.latest_message_at || a.updated_at || a.created_at, b.latest_message_at || b.updated_at || b.created_at));

      if (!rows.length) {
        state.queue.mailThreadIds = [];
        state.queue.mailThreadsById = {};
        listEl.innerHTML = queueListEmpty('No puppy mail threads in this filter.');
        return;
      }

      state.queue.mailThreadIds = rows.map((thread) => Number(thread.id || 0)).filter((id) => id > 0);
      state.queue.mailThreadsById = {};
      rows.forEach((thread) => {
        const id = Number(thread.id || 0);
        if (id) state.queue.mailThreadsById[id] = thread;
      });

      listEl.innerHTML = rows.map((thread) => {
        const id = Number(thread.id || 0);
        const selected = state.queue.selectedMailThreadId === id;
        const senderName = String(thread.sender_name || 'Anonymous').trim() || 'Anonymous';
        const avatar = senderName.charAt(0).toUpperCase();
        const latest = escapeHtml(thread.latest_message || 'No messages yet');
        const latestIso = thread.latest_message_at || thread.updated_at || thread.created_at;
        const latestTs = parseTimeValue(latestIso);
        const seenTs = Number(state.queue.mailSeenAtByThread[id] || 0);
        const unread = latestTs > seenTs && !selected;
        const updatedAt = escapeHtml(fmtChatTime(latestIso));
        const selectedClass = selected ? 'hp2-chat-thread-btn-active' : '';
        const status = String(thread.status || 'open').toLowerCase();
        const rowActionStatus = status === 'resolved' ? 'open' : 'resolved';
        const rowActionLabel = status === 'resolved' ? 'Reopen' : 'Resolve';
        const unreadBadge = unread ? '<span class="hp2-chat-unread-badge">New</span>' : '';
        return `<li>
          <button type="button" class="hp2-chat-thread-btn ${selectedClass}" data-q-action="mail-open" data-id="${id}">
            <div class="hp2-chat-thread-row">
              <span class="hp2-chat-avatar">${escapeHtml(avatar)}</span>
              <div>
                <p class="hp2-chat-thread-name">${escapeHtml(senderName)}</p>
                <p class="hp2-chat-thread-preview">${latest}</p>
              </div>
              <div class="hp2-chat-thread-side">
                <span class="hp2-chat-thread-time">${updatedAt}</span>
                ${unreadBadge}
              </div>
            </div>
          </button>
          <div class="hp2-chat-thread-actions">
            <button type="button" class="hp2-btn hp2-btn-ghost" data-q-action="mail-row-status" data-id="${id}" data-status="${rowActionStatus}">${rowActionLabel}</button>
          </div>
        </li>`;
      }).join('');
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        listEl.innerHTML = queueListEmpty('Failed to load puppy mail threads.');
      }
    }
  }

  function renderQueueMailMessages(messages) {
    const host = byId('hp2-queue-mail-messages');
    const threadId = Number(state.queue.selectedMailThreadId || 0);
    state.queue.mailMessagesById = {};
    const safeMessages = Array.isArray(messages) ? messages : [];
    const pending = getPendingMessages(threadId);
    if (!safeMessages.length && !pending.length) {
      host.innerHTML = queueListEmpty('No messages in this thread.');
      return;
    }

    const timeline = [];
    safeMessages.forEach((m) => {
      const id = Number(m.id || 0);
      if (id) state.queue.mailMessagesById[id] = m;
      const author = String(m.author || '').trim() || 'Unknown';
      const lower = author.toLowerCase();
      const authoredByHandler = lower.includes('handler') || lower.includes('admin') || lower.includes('operator');
      timeline.push({
        key: `srv-${id || Math.random().toString(36).slice(2, 8)}`,
        id,
        author,
        body: String(m.body || ''),
        createdAt: String(m.created_at || ''),
        own: authoredByHandler,
        pending: false,
        delivery: 'sent',
      });
    });

    pending.forEach((m) => {
      timeline.push({
        key: m.tempId,
        id: 0,
        tempId: m.tempId,
        author: String(m.author || 'Handler'),
        body: String(m.body || ''),
        createdAt: String(m.created_at || ''),
        own: true,
        pending: true,
        delivery: String(m.delivery || 'sending'),
        error: String(m.error || ''),
      });
    });

    timeline.sort((a, b) => parseTimeValue(a.createdAt) - parseTimeValue(b.createdAt));

    const html = [];
    let previousDay = '';
    for (let i = 0; i < timeline.length; i += 1) {
      const row = timeline[i];
      const prev = timeline[i - 1] || null;
      const next = timeline[i + 1] || null;
      const dayLabel = fmtChatDayLabel(row.createdAt);
      if (dayLabel !== previousDay) {
        previousDay = dayLabel;
        html.push(`<li class="hp2-chat-date-separator"><span>${escapeHtml(dayLabel)}</span></li>`);
      }

      const groupedWithPrev = !!prev && prev.own === row.own && prev.author === row.author && (parseTimeValue(row.createdAt) - parseTimeValue(prev.createdAt)) < 300000;
      const groupedWithNext = !!next && next.own === row.own && next.author === row.author && (parseTimeValue(next.createdAt) - parseTimeValue(row.createdAt)) < 300000;
      const bubbleClass = `${row.own ? 'hp2-chat-self' : ''} ${groupedWithPrev ? 'hp2-chat-bubble-grouped' : ''}`.trim();

      let deliveryText = 'Sent';
      if (row.pending && row.delivery === 'sending') deliveryText = 'Sending...';
      if (row.pending && row.delivery === 'failed') deliveryText = 'Failed';

      const editAction = (!row.pending && row.id)
        ? ` • <button type="button" class="hp2-btn hp2-btn-ghost" data-q-action="mail-edit" data-id="${row.id}">Edit</button>`
        : '';
      const retryAction = (row.pending && row.delivery === 'failed')
        ? ` • <button type="button" class="hp2-btn hp2-btn-ghost" data-q-action="mail-retry" data-temp-id="${escapeHtml(row.tempId || '')}">Retry</button>`
        : '';

      const meta = groupedWithNext
        ? ''
        : `<div class="hp2-chat-message-meta">${escapeHtml(row.author)} • ${escapeHtml(fmtChatTime(row.createdAt))} • ${escapeHtml(deliveryText)}${editAction}${retryAction}</div>`;

      html.push(`<li class="${bubbleClass}">
        <p class="hp2-chat-message-body">${escapeHtml(row.body)}</p>
        ${meta}
      </li>`);
    }

    host.innerHTML = html.join('');
    host.scrollTop = host.scrollHeight;
  }

  async function loadQueueMailThread(threadId) {
    state.queue.selectedMailThreadId = threadId;
    const title = byId('hp2-queue-mail-title');
    const meta = byId('hp2-queue-mail-meta');
    if (title) title.textContent = 'Loading thread...';
    meta.textContent = 'Loading thread...';
    try {
      const data = await apiGet(`/api/handler/puppy-mail/threads/${encodeURIComponent(String(threadId))}`);
      const thread = data.thread || {};
      if (title) title.textContent = thread.sender_name || `Thread #${thread.id || threadId}`;
      meta.textContent = `Thread #${thread.id || threadId} • ${thread.status || 'open'}`;
      state.queue.selectedMailMessages = Array.isArray(data.messages) ? data.messages : [];
      const newest = state.queue.selectedMailMessages.length
        ? state.queue.selectedMailMessages[state.queue.selectedMailMessages.length - 1].created_at
        : (thread.latest_message_at || thread.updated_at || thread.created_at || new Date().toISOString());
      markThreadSeen(threadId, newest);
      renderQueueMailMessages(state.queue.selectedMailMessages);
      await loadQueueMailThreads();
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        if (title) title.textContent = 'Thread Detail';
        meta.textContent = 'Failed to load thread.';
        state.queue.selectedMailMessages = [];
        renderQueueMailMessages(state.queue.selectedMailMessages);
      }
    }
  }

  async function sendQueueMailReply() {
    const threadId = state.queue.selectedMailThreadId;
    if (!threadId) {
      setQueueResult('Select a mail thread first.');
      return;
    }
    const input = byId('hp2-queue-mail-reply');
    const body = String(input?.value || '').trim();
    if (!body) {
      setQueueResult('Reply body cannot be empty.');
      return;
    }
    const tempId = addPendingMessage(threadId, body);
    renderQueueMailMessages(state.queue.selectedMailMessages);
    setQueueResult('Sending reply...');
    try {
      await apiPost(`/api/handler/puppy-mail/threads/${encodeURIComponent(String(threadId))}/reply`, { body });
      removePendingMessage(threadId, tempId);
      if (input) {
        input.value = '';
        autoSizeMailComposer();
      }
      setQueueResult('Reply sent.');
      await loadQueueMailThread(threadId);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        patchPendingMessage(threadId, tempId, {
          delivery: 'failed',
          error: err.message,
        });
        renderQueueMailMessages(state.queue.selectedMailMessages);
        setQueueResult(`Failed to send reply: ${err.message}`);
      }
    }
  }

  async function retryQueuePendingMail(tempId) {
    const threadId = Number(state.queue.selectedMailThreadId || 0);
    if (!threadId || !tempId) return;
    const row = getPendingMessages(threadId).find((m) => m.tempId === tempId);
    if (!row) return;
    patchPendingMessage(threadId, tempId, { delivery: 'sending', error: '' });
    renderQueueMailMessages(state.queue.selectedMailMessages);
    setQueueResult('Retrying message...');
    try {
      await apiPost(`/api/handler/puppy-mail/threads/${encodeURIComponent(String(threadId))}/reply`, { body: row.body });
      removePendingMessage(threadId, tempId);
      setQueueResult('Reply sent.');
      await loadQueueMailThread(threadId);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        patchPendingMessage(threadId, tempId, { delivery: 'failed', error: err.message });
        renderQueueMailMessages(state.queue.selectedMailMessages);
        setQueueResult(`Retry failed: ${err.message}`);
      }
    }
  }

  async function updateQueueMailStatusForThread(threadId, status) {
    const id = Number(threadId || 0);
    if (!id) return;
    setQueueResult('Saving thread status...');
    try {
      await apiPost(`/api/handler/puppy-mail/threads/${encodeURIComponent(String(id))}/status`, { status });
      setQueueResult(`Thread marked ${status}.`);
      if (state.queue.selectedMailThreadId === id) {
        await loadQueueMailThread(id);
      } else {
        await loadQueueMailThreads();
      }
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setQueueResult(`Failed to update thread: ${err.message}`);
      }
    }
  }

  async function editQueueMailMessage(messageId) {
    const threadId = state.queue.selectedMailThreadId;
    const message = state.queue.mailMessagesById[Number(messageId)] || null;
    if (!threadId || !message) {
      setQueueResult('Select a thread message first.');
      return;
    }
    const response = await askInlineText({
      title: 'Edit Puppy Mail Message',
      message: 'Update this message text.',
      inputLabel: 'Message',
      inputValue: String(message.body || ''),
      multiline: true,
      confirmText: 'Save',
    });
    if (!response.confirmed) return;
    if (response.invalidEmpty) {
      setQueueResult('Edited message cannot be empty.');
      return;
    }
    const normalized = response.value;
    setQueueResult('Saving edit...');
    try {
      await apiPost(`/api/handler/puppy-mail/messages/${encodeURIComponent(String(messageId))}/edit`, {
        body: normalized,
        author: String(message.author || 'Puppy').trim() || 'Puppy',
      });
      setQueueResult('Message updated.');
      await loadQueueMailThread(threadId);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setQueueResult(`Failed to edit message: ${err.message}`);
      }
    }
  }

  async function updateQueueMailStatus(status) {
    const threadId = state.queue.selectedMailThreadId;
    if (!threadId) {
      setQueueResult('Select a mail thread first.');
      return;
    }
    setQueueResult('Saving thread status...');
    try {
      await apiPost(`/api/handler/puppy-mail/threads/${encodeURIComponent(String(threadId))}/status`, { status });
      setQueueResult(`Thread marked ${status}.`);
      await loadQueueMailThread(threadId);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setQueueResult(`Failed to update thread: ${err.message}`);
      }
    }
  }

  async function loadQueueQuestions() {
    const openEl = byId('hp2-queue-questions-open');
    const answeredEl = byId('hp2-queue-questions-answered');
    openEl.innerHTML = queueListEmpty('Loading unanswered questions...');
    answeredEl.innerHTML = queueListEmpty('Loading answered questions...');
    try {
      const [openRows, answeredRows] = await Promise.all([
        apiGet('/api/handler/questions').catch(() => []),
        apiGet('/api/handler/questions/answered').catch(() => []),
      ]);
      let open = Array.isArray(openRows) ? openRows : [];
      let answered = Array.isArray(answeredRows) ? answeredRows : [];

      open = open.filter((q) => includeBySharedFilter('open', 'open-only'));
      answered = answered.filter((q) => includeBySharedFilter('answered', 'resolved-only'));

      open.sort((a, b) => compareByTimeDescOrAsc(a.created_at, b.created_at));
      answered.sort((a, b) => compareByTimeDescOrAsc(a.created_at, b.created_at));

      state.queue.openQuestions = open;
      state.queue.answeredQuestions = answered;
      state.queue.openVisible = Math.max(3, Math.min(state.queue.openVisible, open.length || 3));
      state.queue.answeredVisible = Math.max(3, Math.min(state.queue.answeredVisible, answered.length || 3));

      renderQueueQuestions();
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        openEl.innerHTML = queueListEmpty('Failed to load questions.');
        answeredEl.innerHTML = queueListEmpty('Failed to load answered questions.');
      }
    }
  }

  function renderQueueQuestions() {
    const openEl = byId('hp2-queue-questions-open');
    const answeredEl = byId('hp2-queue-questions-answered');
    const openOlderBtn = byId('hp2-queue-questions-open-older');
    const answeredOlderBtn = byId('hp2-queue-questions-answered-older');

    const open = state.queue.openQuestions.slice(0, state.queue.openVisible);
    const answered = state.queue.answeredQuestions.slice(0, state.queue.answeredVisible);

    openEl.innerHTML = open.length ? open.map((q) => {
      const id = Number(q.id || 0);
      return `<li>
        <div class="hp2-feed-item-row">
          <strong>${escapeHtml(q.text || '')}</strong>
          <span class="hp2-muted">${escapeHtml(fmtDate(q.created_at))}</span>
        </div>
        <div class="hp2-queue-actions">
          <button type="button" class="hp2-btn hp2-btn-ghost" data-q-action="question-answer" data-id="${id}">Answer</button>
          <button type="button" class="hp2-btn hp2-btn-ghost" data-q-action="question-delete" data-id="${id}">Delete</button>
        </div>
      </li>`;
    }).join('') : queueListEmpty('No unanswered questions.');

    answeredEl.innerHTML = answered.length ? answered.map((q) => {
      const id = Number(q.id || 0);
      return `<li>
        <div class="hp2-feed-item-row">
          <strong>${escapeHtml(q.text || '')}</strong>
          <span class="hp2-muted">${escapeHtml(fmtDate(q.created_at))}</span>
        </div>
        <div class="hp2-muted">${escapeHtml(q.answer || '')}</div>
        <div class="hp2-queue-actions">
          <button type="button" class="hp2-btn hp2-btn-ghost" data-q-action="question-delete" data-id="${id}">Delete</button>
        </div>
      </li>`;
    }).join('') : queueListEmpty('No answered questions.');

    openOlderBtn.style.display = state.queue.openVisible < state.queue.openQuestions.length ? 'inline-flex' : 'none';
    answeredOlderBtn.style.display = state.queue.answeredVisible < state.queue.answeredQuestions.length ? 'inline-flex' : 'none';
  }

  async function answerQueueQuestion(questionId) {
    const response = await askInlineText({
      title: 'Answer Question',
      message: 'Write an answer to publish to this question.',
      inputLabel: 'Answer',
      inputPlaceholder: 'Type answer...',
      multiline: true,
      confirmText: 'Publish',
    });
    if (!response.confirmed) return;
    if (response.invalidEmpty) {
      setQueueResult('Answer cannot be empty.');
      return;
    }
    const normalized = response.value;
    setQueueResult('Publishing answer...');
    try {
      await apiPost(`/api/handler/questions/${encodeURIComponent(String(questionId))}/answer`, { answer: normalized });
      setQueueResult('Answer published.');
      await loadQueueQuestions();
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setQueueResult(`Failed to answer question: ${err.message}`);
      }
    }
  }

  async function deleteQueueQuestion(questionId) {
    const response = await askInlineConfirm({
      title: 'Delete Question',
      message: 'Delete this question permanently? This cannot be undone.',
      confirmText: 'Delete',
      danger: true,
    });
    if (!response.confirmed) return;
    setQueueResult('Deleting question...');
    try {
      await apiFetch(`/api/handler/questions/${encodeURIComponent(String(questionId))}`, { method: 'DELETE' });
      setQueueResult('Question deleted.');
      await loadQueueQuestions();
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setQueueResult(`Failed to delete question: ${err.message}`);
      }
    }
  }

  async function loadEvidenceDrawer() {
    const pendingEl = byId('hp2-drawer-limbo-pending');
    const resolvedEl = byId('hp2-drawer-limbo-resolved');
    pendingEl.innerHTML = queueListEmpty('Loading pending limbo items...');
    resolvedEl.innerHTML = queueListEmpty('Loading resolved limbo items...');
    try {
      const [pendingRows, allRows] = await Promise.all([
        apiGet('/api/handler/limbo?status=pending&limit=200').catch(() => []),
        apiGet('/api/handler/limbo?status=all&limit=300').catch(() => []),
      ]);
      let pending = Array.isArray(pendingRows) ? pendingRows : [];
      const all = Array.isArray(allRows) ? allRows : [];
      let resolved = all.filter((item) => item.status !== 'pending');

      pending = pending.filter((item) => includeBySharedFilter(item.status || 'pending', 'open-only'));
      resolved = resolved.filter((item) => includeBySharedFilter(item.status || 'resolved', 'resolved-only'));

      pending.sort((a, b) => compareByTimeDescOrAsc(a.created_at, b.created_at));
      resolved.sort((a, b) => compareByTimeDescOrAsc(a.created_at, b.created_at));

      pendingEl.innerHTML = pending.length ? pending.map((item) => {
        const id = Number(item.id || 0);
        return `<li>
          <div class="hp2-feed-item-row">
            <strong>${escapeHtml(item.prompt_text || '')}</strong>
            <span class="${severityClass('warning')}">${escapeHtml(item.source || 'handler')}</span>
          </div>
          <div class="hp2-muted">${escapeHtml(fmtDate(item.created_at))}</div>
          <div class="hp2-queue-actions">
            <button type="button" class="hp2-btn hp2-btn-ghost" data-q-action="limbo-answer" data-id="${id}">Answer</button>
            <button type="button" class="hp2-btn hp2-btn-ghost" data-q-action="limbo-dismiss" data-id="${id}">Dismiss</button>
          </div>
        </li>`;
      }).join('') : queueListEmpty('No pending limbo items.');

      resolvedEl.innerHTML = resolved.length ? resolved.slice(0, 120).map((item) => {
        const id = Number(item.id || 0);
        const resolution = item.status === 'answered' ? (item.answer_text || 'Answered') : (item.dismissed_reason || 'Dismissed');
        return `<li>
          <div class="hp2-feed-item-row">
            <strong>${escapeHtml(item.prompt_text || '')}</strong>
            <span class="${severityClass(item.status === 'answered' ? 'info' : 'warning')}">${escapeHtml(item.status || 'resolved')}</span>
          </div>
          <div class="hp2-muted">${escapeHtml(resolution)}</div>
          <div class="hp2-queue-actions">
            ${item.status === 'answered' && !item.published_question_id ? `<button type="button" class="hp2-btn hp2-btn-ghost" data-q-action="limbo-publish" data-id="${id}">Publish</button>` : ''}
          </div>
        </li>`;
      }).join('') : queueListEmpty('No resolved limbo items.');
      state.telemetry.drawerAt = Date.now();
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        pendingEl.innerHTML = queueListEmpty('Failed to load pending limbo.');
        resolvedEl.innerHTML = queueListEmpty('Failed to load resolved limbo.');
      }
    }
  }

  async function answerQueueLimbo(itemId) {
    const response = await askInlineText({
      title: 'Answer Evidence Drawer Item',
      message: 'Add an answer for this pending item.',
      inputLabel: 'Answer',
      inputPlaceholder: 'Type answer...',
      multiline: true,
      confirmText: 'Save Answer',
    });
    if (!response.confirmed) return;
    if (response.invalidEmpty) {
      setQueueResult('Limbo answer cannot be empty.');
      return;
    }
    const normalized = response.value;
    setQueueResult('Saving limbo answer...');
    try {
      await apiPost(`/api/handler/limbo/${encodeURIComponent(String(itemId))}/answer`, { answer_text: normalized });
      setQueueResult('Limbo item answered.');
      await loadEvidenceDrawer();
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setQueueResult(`Failed to answer limbo item: ${err.message}`);
      }
    }
  }

  async function dismissQueueLimbo(itemId) {
    const response = await askInlineText({
      title: 'Dismiss Evidence Drawer Item',
      message: 'Optionally provide a reason for dismissal.',
      inputLabel: 'Reason (optional)',
      inputValue: '',
      multiline: true,
      allowEmpty: true,
      confirmText: 'Dismiss',
      danger: true,
    });
    if (!response.confirmed) return;
    const reason = response.value || '';
    setQueueResult('Dismissing limbo item...');
    try {
      await apiPost(`/api/handler/limbo/${encodeURIComponent(String(itemId))}/dismiss`, { reason });
      setQueueResult('Limbo item dismissed.');
      await loadEvidenceDrawer();
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setQueueResult(`Failed to dismiss limbo item: ${err.message}`);
      }
    }
  }

  async function publishQueueLimbo(itemId) {
    setQueueResult('Publishing limbo item...');
    try {
      const result = await apiPost(`/api/handler/limbo/${encodeURIComponent(String(itemId))}/publish`, {});
      if (result && result.already_published) {
        setQueueResult('Limbo item was already published.');
      } else {
        setQueueResult('Limbo item published.');
      }
      await loadEvidenceDrawer();
      await loadQueueQuestions();
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setQueueResult(`Failed to publish limbo item: ${err.message}`);
      }
    }
  }

  async function loadQueueHub() {
    await Promise.allSettled([
      loadQueueBooking(),
      loadQueueMailThreads(),
      loadQueueQuestions(),
    ]);
    state.telemetry.queueAt = Date.now();
  }

  async function handleQueueActionClick(event) {
    const btn = event.target.closest('button[data-q-action]');
    if (!btn) return;
    const action = btn.dataset.qAction;
    const id = Number(btn.dataset.id || 0);
    if (action === 'booking-status') {
      await updateBookingStatus(id, btn.dataset.status || 'new');
      return;
    }
    if (action === 'mail-open') {
      await loadQueueMailThread(id);
      return;
    }
    if (action === 'mail-edit') {
      await editQueueMailMessage(id);
      return;
    }
    if (action === 'mail-retry') {
      await retryQueuePendingMail(btn.dataset.tempId || '');
      return;
    }
    if (action === 'mail-row-status') {
      await updateQueueMailStatusForThread(id, btn.dataset.status || 'open');
      return;
    }
    if (action === 'question-answer') {
      await answerQueueQuestion(id);
      return;
    }
    if (action === 'question-delete') {
      await deleteQueueQuestion(id);
      return;
    }
    if (action === 'limbo-answer') {
      await answerQueueLimbo(id);
      return;
    }
    if (action === 'limbo-dismiss') {
      await dismissQueueLimbo(id);
      return;
    }
    if (action === 'limbo-publish') {
      await publishQueueLimbo(id);
    }
  }

  async function lockSelected() {
    if (!state.selectedDeviceId) {
      setInlineResult('hp2-action-result', 'Select a device first.');
      return;
    }
    const confirm = await askInlineConfirm({
      title: 'Lock Device',
      message: `Lock ${state.selectedDeviceId}?`,
      confirmText: 'Lock',
      danger: true,
    });
    if (!confirm.confirmed) return;

    setInlineResult('hp2-action-result', 'Sending lock...');
    try {
      await apiPost('/api/handler/lock', { device_id: state.selectedDeviceId });
      setInlineResult('hp2-action-result', 'Lock sent.');
      pushFeed(`Lock command sent for ${state.selectedDeviceId}`);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult('hp2-action-result', `Failed: ${err.message}`);
      }
    }
  }

  async function requestCheckin() {
    if (!state.selectedDeviceId) {
      setInlineResult('hp2-action-result', 'Select a device first.');
      return;
    }
    const confirm = await askInlineConfirm({
      title: 'Request Check-In',
      message: `Send check-in request to ${state.selectedDeviceId}?`,
      confirmText: 'Request',
    });
    if (!confirm.confirmed) return;

    setInlineResult('hp2-action-result', 'Requesting check-in...');
    try {
      await apiPost('/api/handler/tpe/checkins/request', { device_id: state.selectedDeviceId });
      setInlineResult('hp2-action-result', 'Check-in requested.');
      recordCommandHistory('Request Check-In', `Requested for ${selectedDeviceLabel()}`, true);
      pushFeed(`Check-in requested for ${state.selectedDeviceId}`);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult('hp2-action-result', `Failed: ${err.message}`);
        recordCommandHistory('Request Check-In', `Failed for ${selectedDeviceLabel()}: ${err.message}`, false);
      }
    }
  }

  async function sendQuickActionCommand({
    title,
    confirmText,
    action,
    payload,
    successMessage,
    historyDetail,
  }) {
    if (!state.selectedDeviceId) {
      setInlineResult('hp2-action-result', 'Select a device first.');
      return;
    }

    const label = selectedDeviceLabel();
    const confirmed = await askInlineConfirm({
      title,
      message: `${title} for ${label}?`,
      confirmText,
      danger: action === 'PAVLOK_COMMAND' && String(payload?.pavlok_cmd || '').toLowerCase() === 'shock',
    });
    if (!confirmed.confirmed) return;

    setInlineResult('hp2-action-result', `${title} sending...`);
    try {
      await apiPost('/api/handler/tpe/push', {
        device_id: state.selectedDeviceId,
        action,
        payload,
        ...payload,
      });
      setInlineResult('hp2-action-result', successMessage);
      recordCommandHistory(title, historyDetail || `${title} for ${label}`, true);
      pushFeed(`${title} sent to ${state.selectedDeviceId}`);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult('hp2-action-result', `Failed: ${err.message}`);
        recordCommandHistory(title, `${title} failed for ${label}: ${err.message}`, false);
      }
    }
  }

  async function sendControlCommand({
    title,
    action,
    fields = {},
    resultId,
    confirmText = 'Send',
    danger = false,
    message,
    historyDetail,
  }) {
    if (!state.selectedDeviceId) {
      setInlineResult(resultId, 'Select a device first.');
      return;
    }

    const confirmed = await askInlineConfirm({
      title,
      message: message || `${title} for ${selectedDeviceLabel()}?`,
      confirmText,
      danger,
    });
    if (!confirmed.confirmed) return;

    setInlineResult(resultId, `${title} sending...`);
    try {
      await apiPost('/api/handler/tpe/push', {
        device_id: state.selectedDeviceId,
        action,
        ...fields,
      });
      setInlineResult(resultId, `${title} sent.`);
      recordCommandHistory(title, historyDetail || `${action} for ${selectedDeviceLabel()}`, true);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult(resultId, `Failed: ${err.message}`);
        recordCommandHistory(title, `${title} failed: ${err.message}`, false);
      }
    }
  }

  async function sendAppLifecycleCommand() {
    const action = String(byId('hp2-appctl-action')?.value || '').trim();
    const appName = String(byId('hp2-appctl-name')?.value || '').trim();
    if (!action) {
      setInlineResult('hp2-appctl-result', 'Choose an app action first.');
      return;
    }
    if (!appName) {
      setInlineResult('hp2-appctl-result', 'App name is required.');
      return;
    }
    const title = action.replaceAll('_', ' ');
    const dangerActions = new Set(['FORCE_STOP_APP', 'DISABLE_APP', 'UNINSTALL_APP', 'SUSPEND_APP']);
    await sendControlCommand({
      title,
      action,
      fields: { app_name: appName },
      resultId: 'hp2-appctl-result',
      confirmText: 'Send',
      danger: dangerActions.has(action),
      message: `${title} for ${appName} on ${selectedDeviceLabel()}?`,
      historyDetail: `${title} app=${appName}`,
    });
  }

  async function sendScreenLockAction(action, opts = {}) {
    await sendControlCommand({
      title: opts.title || action.replaceAll('_', ' '),
      action,
      fields: opts.fields || {},
      resultId: 'hp2-screenctl-result',
      confirmText: opts.confirmText || 'Send',
      danger: !!opts.danger,
      message: opts.message,
      historyDetail: opts.historyDetail,
    });
  }

  async function sendQuickTapCommand() {
    if (!state.selectedDeviceId) {
      setInlineResult('hp2-quicktap-result', 'Select a device first.');
      return;
    }
    const target = state.commands.liveControl.quickTapTarget;
    const action = state.commands.liveControl.quickTapAction;
    const intensity = Number(byId('hp2-quicktap-intensity')?.value || 10);
    const length = Number(byId('hp2-quicktap-length')?.value || 800);
    const loop = Math.max(1, Math.min(12, Number(byId('hp2-quicktap-loop')?.value || 1)));

    const confirm = await askInlineConfirm({
      title: 'Send Quick Tap',
      message: `${target} ${action} intensity ${intensity} loop ${loop} for ${selectedDeviceLabel()}?`,
      confirmText: 'Send',
      danger: target === 'pavlok' && action === 'shock',
    });
    if (!confirm.confirmed) return;

    setInlineResult('hp2-quicktap-result', 'Sending quick tap...');
    try {
      for (let i = 0; i < loop; i += 1) {
        if (target === 'pavlok') {
          await apiPost('/api/handler/tpe/push', {
            device_id: state.selectedDeviceId,
            action: 'PAVLOK_COMMAND',
            payload: {
              pavlok_cmd: action,
              pavlok_intensity: String(intensity),
              intensity: String(intensity),
              toy_level: String(intensity),
              ...(action !== 'shock' ? {
                pavlok_duration_ms: String(length),
                duration_ms: String(length),
                toy_duration_ms: String(length),
              } : {}),
            },
            pavlok_cmd: action,
            pavlok_intensity: String(intensity),
            intensity: String(intensity),
            toy_level: String(intensity),
            ...(action !== 'shock' ? {
              pavlok_duration_ms: String(length),
              duration_ms: String(length),
              toy_duration_ms: String(length),
            } : {}),
          });
        } else {
          await apiPost('/api/handler/tpe/push', {
            device_id: state.selectedDeviceId,
            action: 'LOVENSE_COMMAND',
            payload: {
              command: action,
              toy_command: action,
              intensity,
              toy_level: intensity,
              level: intensity,
              length,
              duration_ms: length,
              toy_duration_ms: length,
            },
            command: action,
            toy_command: action,
            intensity,
            toy_level: intensity,
            level: intensity,
            length,
            duration_ms: length,
            toy_duration_ms: length,
          });
        }
      }
      setInlineResult('hp2-quicktap-result', `Quick tap sent (${loop}x).`);
      recordCommandHistory('Quick Tap', `${target} ${action} intensity ${intensity}${action !== 'shock' ? ` length ${length}ms` : ''} loop ${loop}`, true);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult('hp2-quicktap-result', `Failed: ${err.message}`);
        recordCommandHistory('Quick Tap', `${target} ${action} failed: ${err.message}`, false);
      }
    }
  }

  async function sendLovenseLivePattern() {
    if (!state.selectedDeviceId) {
      setInlineResult('hp2-lovense-result', 'Select a device first.');
      return;
    }
    const pattern = String(byId('hp2-lovense-pattern')?.value || '').trim();
    const level = Math.max(1, Math.min(20, Number(byId('hp2-lovense-live-level')?.value || 10)));
    const duration = Math.max(500, Number(byId('hp2-lovense-live-duration')?.value || 5000));
    setInlineResult('hp2-lovense-result', 'Sending Lovense live pattern...');
    try {
      await apiPost('/api/handler/tpe/push', {
        device_id: state.selectedDeviceId,
        action: 'toy.live.control',
        toy_mode: 'lovense',
        toy_command: 'vibrate',
        toy_level: String(level),
        toy_duration_ms: String(duration),
        ...(pattern ? { toy_pattern: pattern } : {}),
      });
      setInlineResult('hp2-lovense-result', 'Lovense live pattern sent.');
      recordCommandHistory('Lovense Live', `${pattern || 'steady'} level ${level} duration ${duration}ms`, true);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult('hp2-lovense-result', `Failed: ${err.message}`);
        recordCommandHistory('Lovense Live', `Failed: ${err.message}`, false);
      }
    }
  }

  async function sendLovenseRamp() {
    if (!state.selectedDeviceId) {
      setInlineResult('hp2-lovense-result', 'Select a device first.');
      return;
    }
    const minLevel = Math.max(1, Math.min(20, Number(byId('hp2-lovense-ramp-min')?.value || 4)));
    const maxLevel = Math.max(minLevel, Math.min(20, Number(byId('hp2-lovense-ramp-max')?.value || 14)));
    const stepMs = Math.max(250, Number(byId('hp2-lovense-ramp-step-ms')?.value || 1200));
    const loops = Math.max(1, Math.min(12, Number(byId('hp2-lovense-ramp-loops')?.value || 3)));

    const sequence = [];
    for (let l = minLevel; l <= maxLevel; l += 1) {
      sequence.push({ level: l, duration_ms: stepMs });
    }
    for (let l = maxLevel - 1; l >= minLevel + 1; l -= 1) {
      sequence.push({ level: l, duration_ms: stepMs });
    }

    const full = [];
    for (let i = 0; i < loops; i += 1) {
      full.push(...sequence);
    }

    setInlineResult('hp2-lovense-result', 'Sending live up/down ramp...');
    try {
      await apiPost('/api/handler/tpe/push', {
        device_id: state.selectedDeviceId,
        action: 'toy.live.control',
        toy_mode: 'lovense',
        toy_command: 'vibrate',
        toy_level: String(minLevel),
        toy_sequence: JSON.stringify(full),
      });
      setInlineResult('hp2-lovense-result', 'Live up/down ramp sent.');
      recordCommandHistory('Lovense Ramp', `min ${minLevel} max ${maxLevel} step ${stepMs}ms loops ${loops}`, true);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult('hp2-lovense-result', `Failed: ${err.message}`);
        recordCommandHistory('Lovense Ramp', `Failed: ${err.message}`, false);
      }
    }
  }

  async function sendLovenseSchedule() {
    if (!state.selectedDeviceId) {
      setInlineResult('hp2-lovense-result', 'Select a device first.');
      return;
    }
    const raw = byId('hp2-lovense-schedule')?.value || '';
    const parsed = parseLovenseSchedules(raw);
    if (!parsed.length) {
      setInlineResult('hp2-lovense-result', 'Add at least one schedule row.');
      return;
    }

    setInlineResult('hp2-lovense-result', 'Sending Lovense schedule...');
    try {
      await apiPost('/api/handler/tpe/push', {
        device_id: state.selectedDeviceId,
        action: 'SET_LOVENSE_SCHEDULES',
        schedules: JSON.stringify(parsed),
      });
      setInlineResult('hp2-lovense-result', 'Lovense schedule sent.');
      recordCommandHistory('Lovense Timed', `${parsed.length} schedule row(s)`, true);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult('hp2-lovense-result', `Failed: ${err.message}`);
        recordCommandHistory('Lovense Timed', `Failed: ${err.message}`, false);
      }
    }
  }

  async function sendPavlokPrecision() {
    if (!state.selectedDeviceId) {
      setInlineResult('hp2-pavlok-result', 'Select a device first.');
      return;
    }
    const cmd = String(byId('hp2-pavlok-command')?.value || 'shock').toLowerCase();
    const intensity = Math.max(0, Math.min(255, Number(byId('hp2-pavlok-intensity')?.value || 60)));
    const duration = Math.max(100, Number(byId('hp2-pavlok-duration')?.value || 1000));

    setInlineResult('hp2-pavlok-result', 'Sending Pavlok command...');
    try {
      await apiPost('/api/handler/tpe/push', {
        device_id: state.selectedDeviceId,
        action: 'PAVLOK_COMMAND',
        payload: {
          pavlok_cmd: cmd,
          pavlok_intensity: String(intensity),
          intensity: String(intensity),
          toy_level: String(intensity),
          ...(cmd !== 'shock' && cmd !== 'stop' ? {
            pavlok_duration_ms: String(duration),
            duration_ms: String(duration),
            toy_duration_ms: String(duration),
          } : {}),
        },
        pavlok_cmd: cmd,
        pavlok_intensity: String(intensity),
        intensity: String(intensity),
        toy_level: String(intensity),
        ...(cmd !== 'shock' && cmd !== 'stop' ? {
          pavlok_duration_ms: String(duration),
          duration_ms: String(duration),
          toy_duration_ms: String(duration),
        } : {}),
      });
      setInlineResult('hp2-pavlok-result', 'Pavlok command sent.');
      recordCommandHistory('Pavlok Precision', `${cmd} intensity ${intensity}${cmd !== 'shock' && cmd !== 'stop' ? ` duration ${duration}ms` : ''}`, true);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult('hp2-pavlok-result', `Failed: ${err.message}`);
        recordCommandHistory('Pavlok Precision', `Failed: ${err.message}`, false);
      }
    }
  }

  async function sendScreenBrightness() {
    const value = Math.max(0, Math.min(255, Number(byId('hp2-screenctl-brightness')?.value || 150)));
    await sendScreenLockAction('SET_BRIGHTNESS', {
      title: 'Set Brightness',
      fields: { value: String(value) },
      message: `Set brightness to ${value} for ${selectedDeviceLabel()}?`,
      historyDetail: `SET_BRIGHTNESS value=${value}`,
    });
  }

  async function sendScreenTimeout() {
    const timeoutMs = Math.max(1000, Math.min(86400000, Number(byId('hp2-screenctl-timeout')?.value || 120000)));
    await sendScreenLockAction('SET_SCREEN_TIMEOUT', {
      title: 'Set Screen Timeout',
      fields: { ms: String(timeoutMs) },
      message: `Set timeout to ${timeoutMs}ms for ${selectedDeviceLabel()}?`,
      historyDetail: `SET_SCREEN_TIMEOUT ms=${timeoutMs}`,
    });
  }

  async function sendAutoRotate() {
    const enabled = String(byId('hp2-screenctl-autorotate')?.value || 'true') === 'true';
    await sendScreenLockAction('SET_AUTO_ROTATE', {
      title: 'Set Auto Rotate',
      fields: { enabled: enabled ? 'true' : 'false' },
      message: `${enabled ? 'Enable' : 'Disable'} auto-rotate for ${selectedDeviceLabel()}?`,
      historyDetail: `SET_AUTO_ROTATE enabled=${enabled}`,
    });
  }

  async function sendOpenUrl() {
    const url = String(byId('hp2-screenctl-url')?.value || '').trim();
    if (!url) {
      setInlineResult('hp2-screenctl-result', 'URL is required.');
      return;
    }
    await sendScreenLockAction('OPEN_URL', {
      title: 'Open URL',
      fields: { url },
      message: `Open URL on ${selectedDeviceLabel()}?`,
      historyDetail: `OPEN_URL ${url}`,
    });
  }

  async function sendNotificationCommand() {
    const title = String(byId('hp2-notify-title')?.value || '').trim();
    const body = String(byId('hp2-notify-body')?.value || '').trim();
    const channelId = String(byId('hp2-notify-channel')?.value || '').trim();
    if (!title) {
      setInlineResult('hp2-notify-result', 'Notification title is required.');
      return;
    }
    await sendControlCommand({
      title: 'Send Notification',
      action: 'SEND_NOTIFICATION',
      fields: {
        title,
        ...(body ? { body } : {}),
        ...(channelId ? { channel_id: channelId } : {}),
      },
      resultId: 'hp2-notify-result',
      message: `Send notification to ${selectedDeviceLabel()}?`,
      historyDetail: `SEND_NOTIFICATION title=${title}`,
    });
  }

  async function clearNotificationsCommand() {
    await sendControlCommand({
      title: 'Clear Notifications',
      action: 'CLEAR_NOTIFICATIONS',
      resultId: 'hp2-notify-result',
      confirmText: 'Clear',
      message: `Clear all notifications on ${selectedDeviceLabel()}?`,
      historyDetail: 'CLEAR_NOTIFICATIONS',
    });
  }

  async function speakTextCommand() {
    const text = String(byId('hp2-speak-text')?.value || '').trim();
    if (!text) {
      setInlineResult('hp2-notify-result', 'Speak text is required.');
      return;
    }
    await sendControlCommand({
      title: 'Speak Text',
      action: 'SPEAK_TEXT',
      fields: { text },
      resultId: 'hp2-notify-result',
      message: `Speak this on ${selectedDeviceLabel()}?`,
      historyDetail: `SPEAK_TEXT ${text.slice(0, 64)}`,
    });
  }

  async function sendClipboardCommand() {
    const text = String(byId('hp2-clipboard-text')?.value || '');
    if (!text.trim()) {
      setInlineResult('hp2-clipboard-sms-result', 'Clipboard text is required.');
      return;
    }
    await sendControlCommand({
      title: 'Set Clipboard',
      action: 'SET_CLIPBOARD',
      fields: { text },
      resultId: 'hp2-clipboard-sms-result',
      message: `Set clipboard content on ${selectedDeviceLabel()}?`,
      historyDetail: `SET_CLIPBOARD ${text.slice(0, 48)}`,
    });
  }

  async function injectProxySmsCommand() {
    const threadId = String(byId('hp2-sms-thread-id')?.value || '').trim() || 'default';
    const body = String(byId('hp2-sms-body')?.value || '').trim();
    const imageUrl = String(byId('hp2-sms-image-url')?.value || '').trim();
    const canReply = String(byId('hp2-sms-can-reply')?.value || 'true');
    if (!body) {
      setInlineResult('hp2-clipboard-sms-result', 'SMS body is required.');
      return;
    }
    await sendControlCommand({
      title: 'Inject Incoming SMS',
      action: 'INCOMING_PROXY_SMS',
      fields: {
        thread_id: threadId,
        body,
        can_reply: canReply,
        ...(imageUrl ? { image_url: imageUrl } : {}),
      },
      resultId: 'hp2-clipboard-sms-result',
      message: `Inject proxy SMS into thread ${threadId} on ${selectedDeviceLabel()}?`,
      historyDetail: `INCOMING_PROXY_SMS thread=${threadId}`,
    });
  }

  async function setSmsReplyPermissionCommand() {
    const threadId = String(byId('hp2-sms-thread-id')?.value || '').trim() || 'default';
    const canReply = String(byId('hp2-sms-can-reply')?.value || 'true');
    await sendControlCommand({
      title: 'Set SMS Reply Permission',
      action: 'SET_SMS_THREAD_CAN_REPLY',
      fields: {
        thread_id: threadId,
        can_reply: canReply,
      },
      resultId: 'hp2-clipboard-sms-result',
      message: `Set can-reply ${canReply} for thread ${threadId} on ${selectedDeviceLabel()}?`,
      historyDetail: `SET_SMS_THREAD_CAN_REPLY thread=${threadId} can_reply=${canReply}`,
    });
  }

  async function refreshSmsThreadPresets() {
    setInlineResult('hp2-clipboard-sms-result', 'Refreshing thread presets...');
    try {
      await loadSmsThreadPresets();
      setInlineResult('hp2-clipboard-sms-result', `Thread presets refreshed (${state.commands.smsThreadPresets.length}).`);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult('hp2-clipboard-sms-result', `Failed to refresh presets: ${err.message}`);
      }
    }
  }

  async function quickBuzz() {
    await sendQuickActionCommand({
      title: 'Quick Buzz',
      confirmText: 'Buzz',
      action: 'LOVENSE_COMMAND',
      payload: {
        command: 'vibrate',
        intensity: 8,
        length: 700,
        level: 8,
        duration_ms: 700,
      },
      successMessage: 'Quick buzz sent.',
      historyDetail: `Buzz to ${selectedDeviceLabel()} intensity 8 length 700ms`,
    });
  }

  async function startleSelected() {
    await sendQuickActionCommand({
      title: 'Startle',
      confirmText: 'Startle',
      action: 'LOVENSE_COMMAND',
      payload: {
        command: 'vibrate',
        intensity: 20,
        length: 500,
        level: 20,
        duration_ms: 500,
      },
      successMessage: 'Startle sent.',
      historyDetail: `Startle to ${selectedDeviceLabel()} at 100% for 500ms`,
    });
  }

  async function shockSelected(level) {
    const intensity = Math.max(0, Math.min(255, Number(level || 0)));
    await sendQuickActionCommand({
      title: `Shock ${intensity}`,
      confirmText: 'Shock',
      action: 'PAVLOK_COMMAND',
      payload: {
        pavlok_cmd: 'shock',
        pavlok_intensity: String(intensity),
        intensity: String(intensity),
        toy_level: String(intensity),
        pavlok_duration_ms: '1000',
        duration_ms: '1000',
        toy_duration_ms: '1000',
      },
      successMessage: `Shock ${intensity} sent.`,
      historyDetail: `Shock ${intensity} to ${selectedDeviceLabel()}`,
    });
  }

  async function renameSelectedDevice() {
    if (!state.selectedDeviceId) {
      setInlineResult('hp2-device-detail-result', 'Select a device first.');
      return;
    }

    const currentName = selectedDevice()?.device_name || '';
    const response = await askInlineText({
      title: 'Set Device Name',
      message: `Set a handler-visible name for ${state.selectedDeviceId}.`,
      confirmText: 'Save',
      inputLabel: 'Device name',
      inputValue: currentName,
    });
    if (!response.confirmed) return;
    if (response.invalidEmpty) {
      setInlineResult('hp2-device-detail-result', 'Name is required.');
      return;
    }

    const nextName = String(response.value || '').trim();
    if (!nextName) {
      setInlineResult('hp2-device-detail-result', 'Name is required.');
      return;
    }

    setInlineResult('hp2-device-detail-result', 'Saving device name...');
    try {
      await apiPatch(`/api/handler/devices/${encodeURIComponent(state.selectedDeviceId)}/name`, {
        device_name: nextName,
      });
      if (state.devices[state.selectedDeviceId]) {
        state.devices[state.selectedDeviceId].device_name = nextName;
      }
      renderDeviceList();
      renderSelectedDevice();
      setInlineResult('hp2-device-detail-result', 'Device name saved.');
      pushFeed(`Renamed ${state.selectedDeviceId} to ${nextName}`);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult('hp2-device-detail-result', `Failed: ${err.message}`);
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
    byId('hp2-quicktap-send-btn').addEventListener('click', () => sendQuickTapCommand().catch(() => {}));
    byId('hp2-lovense-live-start-btn').addEventListener('click', () => sendLovenseLivePattern().catch(() => {}));
    byId('hp2-lovense-ramp-start-btn').addEventListener('click', () => sendLovenseRamp().catch(() => {}));
    byId('hp2-lovense-schedule-send-btn').addEventListener('click', () => sendLovenseSchedule().catch(() => {}));
    byId('hp2-pavlok-send-btn').addEventListener('click', () => sendPavlokPrecision().catch(() => {}));
    byId('hp2-appctl-send-btn').addEventListener('click', () => sendAppLifecycleCommand().catch(() => {}));
    byId('hp2-screenctl-lock-btn').addEventListener('click', () => sendScreenLockAction('LOCK_DEVICE', {
      title: 'Lock Device',
      danger: true,
      confirmText: 'Lock',
      historyDetail: 'LOCK_DEVICE',
    }).catch(() => {}));
    byId('hp2-screenctl-dismiss-keyguard-btn').addEventListener('click', () => sendScreenLockAction('DISMISS_KEYGUARD', {
      title: 'Dismiss Keyguard',
      historyDetail: 'DISMISS_KEYGUARD',
    }).catch(() => {}));
    byId('hp2-screenctl-on-btn').addEventListener('click', () => sendScreenLockAction('SCREEN_ON', {
      title: 'Screen On',
      historyDetail: 'SCREEN_ON',
    }).catch(() => {}));
    byId('hp2-screenctl-off-btn').addEventListener('click', () => sendScreenLockAction('SCREEN_OFF', {
      title: 'Screen Off',
      historyDetail: 'SCREEN_OFF',
    }).catch(() => {}));
    byId('hp2-screenctl-brightness-send-btn').addEventListener('click', () => sendScreenBrightness().catch(() => {}));
    byId('hp2-screenctl-timeout-send-btn').addEventListener('click', () => sendScreenTimeout().catch(() => {}));
    byId('hp2-screenctl-autorotate-send-btn').addEventListener('click', () => sendAutoRotate().catch(() => {}));
    byId('hp2-screenctl-url-send-btn').addEventListener('click', () => sendOpenUrl().catch(() => {}));
    byId('hp2-notify-send-btn').addEventListener('click', () => sendNotificationCommand().catch(() => {}));
    byId('hp2-notify-clear-btn').addEventListener('click', () => clearNotificationsCommand().catch(() => {}));
    byId('hp2-speak-send-btn').addEventListener('click', () => speakTextCommand().catch(() => {}));
    byId('hp2-clipboard-send-btn').addEventListener('click', () => sendClipboardCommand().catch(() => {}));
    byId('hp2-sms-inject-btn').addEventListener('click', () => injectProxySmsCommand().catch(() => {}));
    byId('hp2-sms-reply-toggle-btn').addEventListener('click', () => setSmsReplyPermissionCommand().catch(() => {}));
    byId('hp2-sms-thread-refresh-btn').addEventListener('click', () => refreshSmsThreadPresets().catch(() => {}));
    byId('hp2-startle-btn').addEventListener('click', () => startleSelected().catch(() => {}));
    byId('hp2-shock-10-btn').addEventListener('click', () => shockSelected(10).catch(() => {}));
    byId('hp2-shock-30-btn').addEventListener('click', () => shockSelected(30).catch(() => {}));
    byId('hp2-shock-60-btn').addEventListener('click', () => shockSelected(60).catch(() => {}));
    byId('hp2-rename-device-btn').addEventListener('click', () => renameSelectedDevice().catch(() => {}));
    byId('hp2-refresh-btn').addEventListener('click', () => hydrateApp().catch(() => {}));
    byId('hp2-autofollow-btn').addEventListener('click', toggleAutoFollow);
    byId('hp2-refresh-intel-btn').addEventListener('click', () => loadDashboardIntelligence().catch(() => {}));
    byId('hp2-hard-refresh-btn').addEventListener('click', () => hydrateApp().catch(() => {}));

    document.querySelectorAll('[data-cmd-preset]').forEach((button) => {
      button.addEventListener('click', () => {
        applyCommandPreset(button.dataset.cmdPreset || '');
      });
    });

    document.querySelectorAll('#hp2-quicktap-target-lovense, #hp2-quicktap-target-pavlok').forEach((button) => {
      button.addEventListener('click', () => {
        setQuickTapTarget(button.dataset.quicktapTarget || 'lovense');
      });
    });

    document.querySelectorAll('[data-app-preset]').forEach((button) => {
      button.addEventListener('click', () => {
        byId('hp2-appctl-name').value = button.dataset.appPreset || '';
      });
    });

    ['hp2-quicktap-intensity', 'hp2-lovense-live-level', 'hp2-lovense-ramp-min', 'hp2-lovense-ramp-max', 'hp2-pavlok-intensity', 'hp2-pavlok-command', 'hp2-screenctl-brightness']
      .forEach((id) => {
        const el = byId(id);
        if (!el) return;
        el.addEventListener('input', syncControlReadouts);
        el.addEventListener('change', syncControlReadouts);
      });

    byId('hp2-settings-save-btn').addEventListener('click', () => {
      saveSettingsFromForm();
      setInlineResult('hp2-settings-result', 'Settings saved.');
    });
    byId('hp2-settings-reset-btn').addEventListener('click', () => {
      state.settings = { ...defaultSettings };
      saveSettings();
      renderSettingsForm();
      applyCommandDefaults();
      renderFreshness();
      setInlineResult('hp2-settings-result', 'Settings reset to defaults.');
    });

    byId('hp2-shared-filter-queue').addEventListener('change', (event) => {
      updateSharedControls({ filter: event.target.value });
    });
    byId('hp2-shared-sort-queue').addEventListener('change', (event) => {
      updateSharedControls({ sort: event.target.value });
    });
    byId('hp2-shared-filter-drawer').addEventListener('change', (event) => {
      updateSharedControls({ filter: event.target.value });
    });
    byId('hp2-shared-sort-drawer').addEventListener('change', (event) => {
      updateSharedControls({ sort: event.target.value });
    });

    byId('hp2-queue-booking-filter').addEventListener('change', () => loadQueueBooking().catch(() => {}));
    byId('hp2-queue-mail-filter').addEventListener('change', () => loadQueueMailThreads().catch(() => {}));
    byId('hp2-queue-mail-reply-btn').addEventListener('click', () => sendQueueMailReply().catch(() => {}));
    byId('hp2-queue-mail-details-toggle').addEventListener('click', () => {
      const details = byId('hp2-queue-mail-details');
      if (!details) return;
      setMailDetailsVisible(details.classList.contains('hp2-hidden'));
    });
    byId('hp2-queue-mail-resolve-btn').addEventListener('click', () => updateQueueMailStatus('resolved').catch(() => {}));
    byId('hp2-queue-mail-open-btn').addEventListener('click', () => updateQueueMailStatus('open').catch(() => {}));
    byId('hp2-queue-mail-reply').addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendQueueMailReply().catch(() => {});
      }
    });
    byId('hp2-queue-mail-reply').addEventListener('input', autoSizeMailComposer);

    byId('hp2-modal-cancel-btn').addEventListener('click', () => {
      closeInlineModal({ confirmed: false, value: '' });
    });
    byId('hp2-modal-confirm-btn').addEventListener('click', () => {
      const value = byId('hp2-modal-input')?.value || '';
      closeInlineModal({ confirmed: true, value });
    });
    byId('hp2-modal').addEventListener('click', (event) => {
      if (event.target && event.target.dataset && event.target.dataset.modalDismiss === 'true') {
        closeInlineModal({ confirmed: false, value: '' });
      }
    });

    document.addEventListener('keydown', (event) => {
      if (isInlineModalOpen()) {
        if (event.key === 'Escape') {
          event.preventDefault();
          closeInlineModal({ confirmed: false, value: '' });
          return;
        }
        if (event.key === 'Enter' && !state.modal.multiline && !event.shiftKey) {
          event.preventDefault();
          const value = byId('hp2-modal-input')?.value || '';
          closeInlineModal({ confirmed: true, value });
          return;
        }
      }
      if (isInlineModalOpen()) return;
      if (!byId('hp2-view-queue').classList.contains('hp2-view-active')) return;
      if (event.ctrlKey && event.key === 'ArrowDown') {
        event.preventDefault();
        selectAdjacentMailThread(1);
      }
      if (event.ctrlKey && event.key === 'ArrowUp') {
        event.preventDefault();
        selectAdjacentMailThread(-1);
      }
      if (event.key === '/') {
        const target = event.target;
        if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA')) return;
        event.preventDefault();
        byId('hp2-queue-mail-reply')?.focus();
      }
    });
    byId('hp2-queue-questions-open-older').addEventListener('click', () => {
      state.queue.openVisible += 3;
      renderQueueQuestions();
    });
    byId('hp2-queue-questions-answered-older').addEventListener('click', () => {
      state.queue.answeredVisible += 3;
      renderQueueQuestions();
    });

    ['hp2-queue-booking-list', 'hp2-queue-mail-list', 'hp2-queue-mail-messages', 'hp2-queue-questions-open', 'hp2-queue-questions-answered', 'hp2-drawer-limbo-pending', 'hp2-drawer-limbo-resolved']
      .forEach((id) => {
        byId(id).addEventListener('click', (event) => {
          handleQueueActionClick(event).catch(() => {});
        });
      });

    byId('hp2-device-list').addEventListener('click', handleDeviceListClick);

    document.querySelectorAll('.hp2-tab-btn').forEach((btn) => {
      btn.addEventListener('click', () => setActiveView(btn.dataset.view));
    });
  }

  async function boot() {
    loadSettings();
    bindEvents();
    syncSharedControls();
    setQuickTapTarget('lovense');
    renderQuickTapActionButtons();
    syncControlReadouts();
    renderSmsThreadPresetList();
    renderSettingsForm();
    applyCommandDefaults();
    renderCommandHistory();
    setMailDetailsVisible(false);
    autoSizeMailComposer();
    renderFreshness();
    if (state.telemetry.freshnessTimer) {
      clearInterval(state.telemetry.freshnessTimer);
    }
    state.telemetry.freshnessTimer = setInterval(renderFreshness, 15000);
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
