(() => {
  'use strict';

  const JWT_KEY = 'handler_panel_jwt';
  const SETTINGS_KEY = 'handler_panel_v2_settings';
  const MACROS_KEY = 'handler_panel_v2_macros';
  const AUTH_EXPIRED_ERROR = 'auth-expired';
  const QUEUE_AUTO_REFRESH_MS = 30000;
  const AUTH_REFRESH_MS = 8 * 60 * 1000;
  const COMMAND_ACK_POLL_MS = 7000;
  const views = ['dashboard', 'stats', 'queue', 'drawer', 'devices', 'commands', 'ai-warden', 'public-use', 'settings'];
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
    drawer: {
      mediaFilter: 'all',
      lightboxGallery: [],
      lightboxIndex: -1,
    },
    aiWarden: {
      config: null,
      stats: null,
      reports: [],
      autoRefreshTimer: null,
    },
    publicUse: {
      config: null,
    },
    appInventory: {
      latestSyncId: 0,
      latestSyncAt: null,
      latestPollId: null,
      latestChangedCount: 0,
      latestSource: null,
      apps: [],
      query: {
        search: '',
        includeSystem: true,
      },
    },
    vpnStatus: {
      byDevice: {},
    },
    telemetry: {
      hydratedAt: 0,
      devicesAt: 0,
      intelligenceAt: 0,
      queueAt: 0,
      drawerAt: 0,
      aiWardenAt: 0,
      freshnessTimer: null,
      authRefreshTimer: null,
      commandAckTimer: null,
      liveStatusRefreshTimer: null,
      liveStatusRefreshInFlight: false,
    },
    settings: { ...defaultSettings },
    commands: {
      history: [],
      lastAckEventId: 0,
      metrics: {
        sent: 0,
        executed: 0,
        failed: 0,
      },
      schema: null,
      smsThreadPresets: ['default'],
      macros: [],
      activeSection: 'all',
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

  function loadMacrosLocal() {
    let parsed = [];
    try {
      parsed = JSON.parse(localStorage.getItem(MACROS_KEY) || '[]') || [];
    } catch (_err) {
      parsed = [];
    }
    state.commands.macros = Array.isArray(parsed)
      ? parsed
          .map((row) => ({
            id: String(row?.id || '').trim(),
            name: String(row?.name || '').trim(),
            stepsText: String(row?.stepsText || '').trim(),
          }))
          .filter((row) => row.id && row.name)
      : [];
  }

  function saveMacrosLocal() {
    localStorage.setItem(MACROS_KEY, JSON.stringify(state.commands.macros));
  }

  async function loadMacrosFromServer() {
    try {
      const response = await apiGet('/api/handler/panel-macros');
      const macros = Array.isArray(response?.macros) ? response.macros : [];
      state.commands.macros = macros
        .map((row) => ({
          id: String(row?.id || '').trim(),
          name: String(row?.name || '').trim(),
          stepsText: String(row?.stepsText || row?.steps_text || '').trim(),
        }))
        .filter((row) => row.id && row.name)
        .slice(0, 20);
      saveMacrosLocal();
      renderMacroSelect();
      if (state.commands.macros.length) {
        selectMacroById(state.commands.macros[0].id);
      }
      return true;
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult('hp2-macro-result', 'Using local macros (panel sync unavailable).');
      }
      return false;
    }
  }

  async function syncMacrosToServer() {
    await apiPost('/api/handler/panel-macros', {
      macros: (state.commands.macros || []).map((row) => ({
        id: String(row.id || '').trim(),
        name: String(row.name || '').trim(),
        stepsText: String(row.stepsText || '').trim(),
      })),
    });
  }

  function renderMacroSelect() {
    const select = byId('hp2-macro-select');
    if (!select) return;
    const macros = Array.isArray(state.commands.macros) ? state.commands.macros : [];
    if (!macros.length) {
      select.innerHTML = '<option value="">No macros saved</option>';
      return;
    }
    select.innerHTML = macros.map((macro) => `<option value="${escapeHtml(macro.id)}">${escapeHtml(macro.name)}</option>`).join('');
  }

  function selectMacroById(macroId) {
    const id = String(macroId || '').trim();
    const macro = (state.commands.macros || []).find((row) => row.id === id);
    if (!macro) return;
    if (byId('hp2-macro-name')) byId('hp2-macro-name').value = macro.name;
    if (byId('hp2-macro-steps')) byId('hp2-macro-steps').value = macro.stepsText;
    if (byId('hp2-macro-select')) byId('hp2-macro-select').value = macro.id;
  }

  function parseMacroSteps(raw) {
    return String(raw || '')
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        const parts = line.split(/\s+/).filter(Boolean);
        const action = String(parts.shift() || '').trim().toUpperCase();
        const fields = {};
        parts.forEach((token) => {
          const idx = token.indexOf('=');
          if (idx <= 0) return;
          const key = token.slice(0, idx).trim();
          const value = token.slice(idx + 1).trim();
          if (!key || !value) return;
          fields[key] = value;
        });
        return { action, fields, source: line };
      })
      .filter((step) => step.action);
  }

  async function saveMacroFromInputs() {
    const name = String(byId('hp2-macro-name')?.value || '').trim();
    const stepsText = String(byId('hp2-macro-steps')?.value || '').trim();
    if (!name) {
      setInlineResult('hp2-macro-result', 'Macro name is required.');
      return;
    }
    const steps = parseMacroSteps(stepsText);
    if (!steps.length) {
      setInlineResult('hp2-macro-result', 'Add at least one macro step.');
      return;
    }

    const selectedId = String(byId('hp2-macro-select')?.value || '').trim();
    const existingIndex = state.commands.macros.findIndex((row) => row.id === selectedId);
    if (existingIndex >= 0) {
      state.commands.macros[existingIndex] = {
        ...state.commands.macros[existingIndex],
        name,
        stepsText,
      };
    } else {
      state.commands.macros.unshift({
        id: makeCommandId('macro'),
        name,
        stepsText,
      });
      state.commands.macros = state.commands.macros.slice(0, 20);
    }

    saveMacrosLocal();
    renderMacroSelect();
    selectMacroById(existingIndex >= 0 ? selectedId : state.commands.macros[0].id);
    try {
      await syncMacrosToServer();
      setInlineResult('hp2-macro-result', 'Macro saved.');
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult('hp2-macro-result', 'Macro saved locally. Server sync failed.');
      }
    }
  }

  async function deleteSelectedMacro() {
    const selectedId = String(byId('hp2-macro-select')?.value || '').trim();
    if (!selectedId) {
      setInlineResult('hp2-macro-result', 'Select a macro first.');
      return;
    }
    state.commands.macros = state.commands.macros.filter((row) => row.id !== selectedId);
    saveMacrosLocal();
    renderMacroSelect();
    const first = state.commands.macros[0];
    if (first) {
      selectMacroById(first.id);
    } else {
      if (byId('hp2-macro-name')) byId('hp2-macro-name').value = '';
      if (byId('hp2-macro-steps')) byId('hp2-macro-steps').value = '';
    }
    try {
      await syncMacrosToServer();
      setInlineResult('hp2-macro-result', 'Macro deleted.');
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult('hp2-macro-result', 'Macro deleted locally. Server sync failed.');
      }
    }
  }

  async function runSelectedMacro() {
    if (!state.selectedDeviceId) {
      setInlineResult('hp2-macro-result', 'Select a device first.');
      return;
    }
    const selectedId = String(byId('hp2-macro-select')?.value || '').trim();
    const macro = state.commands.macros.find((row) => row.id === selectedId);
    if (!macro) {
      setInlineResult('hp2-macro-result', 'Select a macro first.');
      return;
    }

    const steps = parseMacroSteps(macro.stepsText);
    if (!steps.length) {
      setInlineResult('hp2-macro-result', 'This macro has no valid steps.');
      return;
    }

    const confirm = await askInlineConfirm({
      title: 'Run Macro',
      message: `Run ${macro.name} (${steps.length} steps) on ${selectedDeviceLabel()}?`,
      confirmText: 'Run',
      danger: steps.some((step) => ['LOCK_DEVICE', 'UNINSTALL_APP', 'DISABLE_APP', 'FORCE_STOP_APP'].includes(step.action)),
    });
    if (!confirm.confirmed) return;

    setInlineResult('hp2-macro-result', `Running ${macro.name}...`);
    let success = 0;
    for (const step of steps) {
      try {
        await apiPost('/api/handler/tpe/push', {
          device_id: state.selectedDeviceId,
          command_id: makeCommandId('macro-step'),
          action: step.action,
          ...step.fields,
        });
        success += 1;
      } catch (err) {
        if (err.message !== AUTH_EXPIRED_ERROR) {
          setInlineResult('hp2-macro-result', `Macro stopped at step ${success + 1}: ${step.source}`);
          recordCommandHistory(`Macro: ${macro.name}`, `Failed at step ${success + 1}: ${step.source}`, false);
        }
        return;
      }
    }

    setInlineResult('hp2-macro-result', `Macro complete: ${success}/${steps.length} steps sent.`);
    recordCommandHistory(`Macro: ${macro.name}`, `${steps.length} steps sent to ${selectedDeviceLabel()}`, true, {
      statusLabel: 'sent',
    });
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

  function normalizeIsoDate(value) {
    const raw = String(value || '').trim();
    if (!raw) return '';
    return raw.slice(0, 10);
  }

  async function loadPublicStatusSettingsForForm() {
    const startInput = byId('hp2-setting-days-start');
    const counterEnabledInput = byId('hp2-setting-counter-discord-enabled');
    const counterChannelInput = byId('hp2-setting-counter-discord-channel');
    const counterEdgeStepInput = byId('hp2-setting-counter-edge-step');
    const edgeTargetCountInput = byId('hp2-setting-edge-target-count');
    const tasksCompletedInput = byId('hp2-setting-tasks-completed');
    const confessionsPostedInput = byId('hp2-setting-confessions-posted');
    const edgeTargetShockAtPeakInput = byId('hp2-setting-edge-target-shock-at-peak');
    const hrEdgeAllowReleaseInput = byId('hp2-setting-hr-edge-allow-release');
    const hrEdgeRampUpInput = byId('hp2-setting-hr-edge-ramp-up');
    const hrEdgeRampDownInput = byId('hp2-setting-hr-edge-ramp-down');
    if (!startInput) return;
    try {
      const data = await apiGet(`/api/handler/public-status?_=${Date.now()}`);
      startInput.value = normalizeIsoDate(data?.days_caged_start_date);
      if (counterEnabledInput) {
        counterEnabledInput.value = data?.discord_counter_notify_enabled ? 'true' : 'false';
      }
      if (counterChannelInput) {
        counterChannelInput.value = String(data?.discord_counter_channel_id || '');
      }
      if (counterEdgeStepInput) {
        const stepRaw = Number(data?.discord_counter_edge_milestone_step || 10);
        const step = Number.isFinite(stepRaw) ? Math.max(1, Math.min(1000, stepRaw)) : 10;
        counterEdgeStepInput.value = String(step);
      }
      if (edgeTargetCountInput) {
        const raw = Number(data?.edge_target_count || 0);
        const target = Number.isFinite(raw) ? Math.max(0, Math.min(1000000, Math.trunc(raw))) : 0;
        edgeTargetCountInput.value = String(target);
      }
      if (tasksCompletedInput) {
        const raw = Number(data?.tasks_completed || 0);
        const value = Number.isFinite(raw) ? Math.max(0, Math.min(1000000, Math.trunc(raw))) : 0;
        tasksCompletedInput.value = String(value);
      }
      if (confessionsPostedInput) {
        const raw = Number(data?.confessions_posted || 0);
        const value = Number.isFinite(raw) ? Math.max(0, Math.min(1000000, Math.trunc(raw))) : 0;
        confessionsPostedInput.value = String(value);
      }
      if (edgeTargetShockAtPeakInput) {
        edgeTargetShockAtPeakInput.value = data?.edge_target_shock_at_peak ? 'true' : 'false';
      }
      if (hrEdgeAllowReleaseInput) {
        hrEdgeAllowReleaseInput.value = data?.hr_edge_allow_release ? 'true' : 'false';
      }
      if (hrEdgeRampUpInput) {
        const raw = Number(data?.hr_edge_ramp_up_step || 2);
        hrEdgeRampUpInput.value = String(Number.isFinite(raw) ? Math.max(1, Math.min(8, Math.trunc(raw))) : 2);
      }
      if (hrEdgeRampDownInput) {
        const raw = Number(data?.hr_edge_ramp_down_step || 3);
        hrEdgeRampDownInput.value = String(Number.isFinite(raw) ? Math.max(1, Math.min(10, Math.trunc(raw))) : 3);
      }
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult('hp2-settings-result', `Failed to load Days Locked start date: ${err.message}`);
      }
    }
  }

  async function saveSettingsFromForm() {
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

    const daysStart = normalizeIsoDate(byId('hp2-setting-days-start')?.value || '');
    const counterEnabled = String(byId('hp2-setting-counter-discord-enabled')?.value || 'false').trim().toLowerCase() === 'true';
    const counterChannelId = String(byId('hp2-setting-counter-discord-channel')?.value || '').trim();
    const counterEdgeStepRaw = Number(byId('hp2-setting-counter-edge-step')?.value || 10);
    const counterEdgeStep = Number.isFinite(counterEdgeStepRaw)
      ? Math.max(1, Math.min(1000, Math.trunc(counterEdgeStepRaw)))
      : 10;
    const edgeTargetCountRaw = Number(byId('hp2-setting-edge-target-count')?.value || 0);
    const edgeTargetCount = Number.isFinite(edgeTargetCountRaw)
      ? Math.max(0, Math.min(1000000, Math.trunc(edgeTargetCountRaw)))
      : 0;
    const tasksCompletedRaw = Number(byId('hp2-setting-tasks-completed')?.value || 0);
    const tasksCompleted = Number.isFinite(tasksCompletedRaw)
      ? Math.max(0, Math.min(1000000, Math.trunc(tasksCompletedRaw)))
      : 0;
    const confessionsPostedRaw = Number(byId('hp2-setting-confessions-posted')?.value || 0);
    const confessionsPosted = Number.isFinite(confessionsPostedRaw)
      ? Math.max(0, Math.min(1000000, Math.trunc(confessionsPostedRaw)))
      : 0;
    const edgeTargetShockAtPeak = String(byId('hp2-setting-edge-target-shock-at-peak')?.value || 'false').trim().toLowerCase() === 'true';
    const hrEdgeAllowRelease = String(byId('hp2-setting-hr-edge-allow-release')?.value || 'false').trim().toLowerCase() === 'true';
    const hrEdgeRampUpRaw = Number(byId('hp2-setting-hr-edge-ramp-up')?.value || 2);
    const hrEdgeRampDownRaw = Number(byId('hp2-setting-hr-edge-ramp-down')?.value || 3);
    const hrEdgeRampUp = Number.isFinite(hrEdgeRampUpRaw) ? Math.max(1, Math.min(8, Math.trunc(hrEdgeRampUpRaw))) : 2;
    const hrEdgeRampDown = Number.isFinite(hrEdgeRampDownRaw) ? Math.max(1, Math.min(10, Math.trunc(hrEdgeRampDownRaw))) : 3;
    let publicStatusSaved = false;
    try {
      await apiPost('/api/handler/public-status', {
        days_caged_start_date: daysStart || null,
        discord_counter_notify_enabled: counterEnabled,
        discord_counter_channel_id: counterChannelId,
        discord_counter_edge_milestone_step: counterEdgeStep,
        edge_target_count: edgeTargetCount,
        tasks_completed: tasksCompleted,
        confessions_posted: confessionsPosted,
        edge_target_shock_at_peak: edgeTargetShockAtPeak,
        hr_edge_allow_release: hrEdgeAllowRelease,
        hr_edge_ramp_up_step: hrEdgeRampUp,
        hr_edge_ramp_down_step: hrEdgeRampDown,
      });
      publicStatusSaved = true;
      await loadPublicStatusSettingsForForm();
    } catch (err) {
      if (err.message === AUTH_EXPIRED_ERROR) {
        throw err;
      }
    }

    return { publicStatusSaved };
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
        <span class="${severityClass(row.ok ? 'info' : 'critical')}">${escapeHtml(row.statusLabel || (row.ok ? 'ok' : 'failed'))}</span>
      </div>
      <div class="hp2-muted">${escapeHtml(row.detail)}</div>
      <div class="hp2-muted">${escapeHtml(fmtDate(row.at))}</div>
    </li>`).join('');
  }

  function renderCommandFeedback() {
    const host = byId('hp2-command-feedback');
    const titleEl = byId('hp2-command-feedback-title');
    const bodyEl = byId('hp2-command-feedback-body');
    if (!host || !titleEl || !bodyEl) return;

    const m = state.commands.metrics || { sent: 0, executed: 0, failed: 0 };
    const latest = state.commands.history[0] || null;
    const latestLabel = latest
      ? `${latest.title}: ${latest.statusLabel || (latest.ok ? 'ok' : 'failed')}`
      : 'No dispatch yet';
    const tone = latest && !latest.ok
      ? 'attention'
      : (latest && String(latest.statusLabel || '').toLowerCase().includes('executed') ? 'ok' : 'pending');

    host.classList.remove('hp2-hidden');
    titleEl.textContent = tone === 'attention' ? 'Command attention required' : (tone === 'ok' ? 'Command executed' : 'Command lane status');
    bodyEl.textContent = `${latestLabel} | sent ${m.sent} | executed ${m.executed} | failed ${m.failed}`;
  }

  function makeCommandId(prefix = 'hp2') {
    const now = Date.now().toString(36);
    const rand = Math.random().toString(36).slice(2, 8);
    return `${prefix}-${now}-${rand}`;
  }

  function recordCommandHistory(title, detail, ok, options = {}) {
    const row = {
      title,
      detail,
      ok: !!ok,
      statusLabel: options.statusLabel || (ok ? 'ok' : 'failed'),
      commandId: options.commandId || null,
      at: new Date().toISOString(),
    };
    state.commands.history.unshift(row);
    state.commands.history = state.commands.history.slice(0, 12);
    state.commands.metrics.sent = Number(state.commands.metrics.sent || 0) + 1;
    if (String(row.statusLabel || '').toLowerCase().includes('failed') || !row.ok) {
      state.commands.metrics.failed = Number(state.commands.metrics.failed || 0) + 1;
    }
    renderCommandHistory();
    renderCommandFeedback();
    return row;
  }

  function markCommandExecuted(commandId, reason) {
    if (!commandId) return;
    let touched = false;
    state.commands.history = state.commands.history.map((row) => {
      if (row.commandId !== commandId) return row;
      touched = true;
      return {
        ...row,
        ok: true,
        statusLabel: 'executed',
        detail: reason ? `${row.detail} | ${reason}` : row.detail,
      };
    });
    if (touched) {
      state.commands.metrics.executed = Number(state.commands.metrics.executed || 0) + 1;
      renderCommandHistory();
      renderCommandFeedback();
    }
  }

  function markCommandFailed(commandId, reason) {
    if (!commandId) return;
    let touched = false;
    state.commands.history = state.commands.history.map((row) => {
      if (row.commandId !== commandId) return row;
      touched = true;
      return {
        ...row,
        ok: false,
        statusLabel: 'failed',
        detail: reason ? `${row.detail} | ${reason}` : row.detail,
      };
    });
    if (touched) {
      state.commands.metrics.failed = Number(state.commands.metrics.failed || 0) + 1;
      renderCommandHistory();
      renderCommandFeedback();
    }
  }

  function quickTapActionsForTarget(target) {
    if (target === 'pavlok') {
      return ['shock', 'vibrate', 'beep', 'stop'];
    }
    return ['vibrate', 'pulse', 'wave', 'tease', 'stop'];
  }

  function normalizePavlokCommand(cmd) {
    const normalized = String(cmd || '').trim().toLowerCase();
    return normalized === 'shock' ? 'zap' : normalized;
  }

  function normalizeLovenseCommand(cmd) {
    const normalized = String(cmd || '').trim().toLowerCase();
    if (normalized === 'pulse' || normalized === 'wave' || normalized === 'tease') {
      return 'vibrate';
    }
    return normalized;
  }

  function waitMs(ms) {
    return new Promise((resolve) => setTimeout(resolve, Math.max(0, Number(ms) || 0)));
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

    const puppyThreads = await apiGet('/api/handler/puppy-mail/threads?status=all&limit=200').catch(() => []);

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

  let authRefreshPromise = null;

  async function refreshAuthToken() {
    if (authRefreshPromise) return authRefreshPromise;
    const jwt = getJwt();
    if (!jwt) return false;

    authRefreshPromise = (async () => {
      try {
        const response = await fetch('/api/auth/refresh', {
          method: 'POST',
          headers: {
            ...authHeader(),
          },
        });
        if (!response.ok) return false;
        const payload = await response.json().catch(() => ({}));
        const nextToken = String(payload?.access_token || '').trim();
        if (!nextToken) return false;
        saveJwt(nextToken);
        return true;
      } catch (_err) {
        return false;
      } finally {
        authRefreshPromise = null;
      }
    })();

    return authRefreshPromise;
  }

  async function apiFetch(path, options = {}, retryAfterRefresh = true) {
    const merged = {
      ...options,
      headers: {
        ...authHeader(),
        ...(options.headers || {}),
      },
    };
    const response = await fetch(path, merged);
    if (response.status === 401) {
      if (retryAfterRefresh && path !== '/api/auth/refresh') {
        const refreshed = await refreshAuthToken();
        if (refreshed) {
          return apiFetch(path, options, false);
        }
      }
      showLogin('Session expired.');
      throw new Error(AUTH_EXPIRED_ERROR);
    }
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      const detail = body?.detail;
      let message = `Request failed (${response.status})`;
      if (typeof detail === 'string' && detail.trim()) {
        message = detail;
      } else if (Array.isArray(detail) && detail.length) {
        message = detail
          .map((item) => {
            if (!item || typeof item !== 'object') return String(item || 'Validation error');
            const loc = Array.isArray(item.loc) ? item.loc.join('.') : '';
            const msg = String(item.msg || '').trim();
            if (loc && msg) return `${loc}: ${msg}`;
            if (msg) return msg;
            try {
              return JSON.stringify(item);
            } catch (_err) {
              return 'Validation error';
            }
          })
          .filter((entry) => String(entry || '').trim())
          .join('; ');
      } else if (detail && typeof detail === 'object') {
        try {
          message = JSON.stringify(detail);
        } catch (_err) {
          message = String(detail);
        }
      }
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

  function applyPushSchemaToUi(schema) {
    applyActionGroupOptions('hp2-appctl-action', schema?.groups?.app_actions);
    applyActionGroupOptions('hp2-screenctl-action', schema?.groups?.screen_actions);
    applyActionGroupOptions('hp2-notify-action', schema?.groups?.notify_actions);
    renderAppActionFieldInputs();
    renderScreenActionFieldInputs();
    renderNotifyActionFieldInputs();
  }

  function applyActionGroupOptions(selectId, rawActions) {
    const select = byId(selectId);
    if (!select) return;
    const actions = Array.isArray(rawActions) ? rawActions : [];
    if (!actions.length) return;
    const allowed = new Set(actions.map((v) => String(v)));
    const current = String(select.value || '');
    Array.from(select.options).forEach((opt) => {
      opt.hidden = !allowed.has(String(opt.value || ''));
    });
    if (!allowed.has(current)) {
      const first = Array.from(select.options).find((opt) => !opt.hidden);
      if (first) select.value = first.value;
    }
  }

  function actionFieldSpecs(action) {
    const fields = state.commands.schema?.action_fields?.[action];
    return Array.isArray(fields) ? fields : [];
  }

  function renderDynamicActionFieldInputs(action, hostId, prefix) {
    const host = byId(hostId);
    if (!host) return;

    const specs = actionFieldSpecs(action);
    if (!specs.length) {
      host.innerHTML = '';
      host.classList.add('hp2-hidden');
      return;
    }

    host.classList.remove('hp2-hidden');
    host.innerHTML = specs.map((spec) => {
      const name = String(spec.name || '').trim();
      const label = String(spec.label || name || 'Field').trim();
      const type = String(spec.type || 'text').trim();
      const required = !!spec.required;
      const placeholder = String(spec.placeholder || '').trim();
      const inputId = `${prefix}-${name}`;

      if (type === 'select' && Array.isArray(spec.options)) {
        const options = spec.options.map((opt) => {
          const value = typeof opt === 'string' ? opt : String(opt?.value || '');
          const text = typeof opt === 'string' ? opt : String(opt?.label || value);
          return `<option value="${escapeHtml(value)}">${escapeHtml(text)}</option>`;
        }).join('');
        return `<label class="hp2-control-field" for="${escapeHtml(inputId)}">
          <span>${escapeHtml(label)}${required ? ' *' : ''}</span>
          <select id="${escapeHtml(inputId)}" data-param="${escapeHtml(name)}" ${required ? 'required' : ''}>${options}</select>
        </label>`;
      }

      if (type === 'textarea') {
        const rows = Number(spec.rows || 2);
        return `<label class="hp2-control-field" for="${escapeHtml(inputId)}">
          <span>${escapeHtml(label)}${required ? ' *' : ''}</span>
          <textarea id="${escapeHtml(inputId)}" data-param="${escapeHtml(name)}" rows="${escapeHtml(String(Math.max(2, Math.min(8, rows))))}" placeholder="${escapeHtml(placeholder)}" ${required ? 'required' : ''}></textarea>
        </label>`;
      }

      const htmlType = ['number', 'url', 'text'].includes(type) ? type : 'text';
      const min = spec.min !== undefined ? ` min="${escapeHtml(String(spec.min))}"` : '';
      const max = spec.max !== undefined ? ` max="${escapeHtml(String(spec.max))}"` : '';
      return `<label class="hp2-control-field" for="${escapeHtml(inputId)}">
        <span>${escapeHtml(label)}${required ? ' *' : ''}</span>
        <input id="${escapeHtml(inputId)}" type="${escapeHtml(htmlType)}" data-param="${escapeHtml(name)}" placeholder="${escapeHtml(placeholder)}" ${required ? 'required' : ''}${min}${max} />
      </label>`;
    }).join('');
  }

  function readDynamicActionFieldValues(action, prefix) {
    const specs = actionFieldSpecs(action);
    const out = {};
    for (const spec of specs) {
      const name = String(spec?.name || '').trim();
      if (!name) continue;
      const el = byId(`${prefix}-${name}`);
      const raw = String(el?.value || '').trim();
      if (!raw) {
        if (spec.required) {
          throw new Error(`${spec.label || name} is required.`);
        }
        continue;
      }
      out[name] = raw;
    }
    return out;
  }

  function renderAppActionFieldInputs() {
    const action = String(byId('hp2-appctl-action')?.value || '').trim();
    const legacyNameWrap = byId('hp2-appctl-name-field');
    const specs = actionFieldSpecs(action);
    if (!specs.length) {
      const host = byId('hp2-appctl-dynamic-fields');
      if (host) {
        host.innerHTML = '';
        host.classList.add('hp2-hidden');
      }
      if (legacyNameWrap) legacyNameWrap.classList.remove('hp2-hidden');
      return;
    }

    if (legacyNameWrap) legacyNameWrap.classList.add('hp2-hidden');
    renderDynamicActionFieldInputs(action, 'hp2-appctl-dynamic-fields', 'hp2-appctl-param');
  }

  function readAppActionFieldValues(action) {
    return readDynamicActionFieldValues(action, 'hp2-appctl-param');
  }

  function renderScreenActionFieldInputs() {
    const action = String(byId('hp2-screenctl-action')?.value || '').trim();
    const legacyWrap = byId('hp2-screenctl-legacy-fields');
    const specs = actionFieldSpecs(action);
    if (legacyWrap) legacyWrap.classList.toggle('hp2-hidden', specs.length > 0);
    renderDynamicActionFieldInputs(action, 'hp2-screenctl-dynamic-fields', 'hp2-screenctl-param');
  }

  function readScreenActionFieldValues(action) {
    return readDynamicActionFieldValues(action, 'hp2-screenctl-param');
  }

  function renderNotifyActionFieldInputs() {
    const action = String(byId('hp2-notify-action')?.value || '').trim();
    const legacyWrap = byId('hp2-notify-legacy-fields');
    const specs = actionFieldSpecs(action);
    if (legacyWrap) legacyWrap.classList.toggle('hp2-hidden', specs.length > 0);
    renderDynamicActionFieldInputs(action, 'hp2-notify-dynamic-fields', 'hp2-notify-param');
  }

  function readNotifyActionFieldValues(action) {
    return readDynamicActionFieldValues(action, 'hp2-notify-param');
  }

  async function loadPushSchema() {
    try {
      const schema = await apiGet('/api/handler/tpe/schema');
      state.commands.schema = schema;
      applyPushSchemaToUi(schema);
    } catch (_err) {
      // Keep built-in static action options as fallback.
    }
  }

  async function pollCommandAcks() {
    try {
      const rows = await apiGet('/api/handler/tpe/events?limit=200');
      if (!Array.isArray(rows)) return;
      rows
        .slice()
        .reverse()
        .forEach((row) => {
          const id = Number(row?.id || 0);
          if (!id || id <= Number(state.commands.lastAckEventId || 0)) return;
          state.commands.lastAckEventId = id;
          const eventType = String(row?.event || '').trim();
          if (eventType !== 'mdm_executed' && eventType !== 'mdm_failed') return;
          let payload = null;
          try {
            payload = typeof row.payload_json === 'string' ? JSON.parse(row.payload_json) : null;
          } catch (_err) {
            payload = null;
          }
          const commandId = String(payload?.command_id || '').trim();
          const command = String(payload?.command || row?.reason || '').trim();
          const reason = String(payload?.reason || row?.reason || '').trim();
          const status = String(payload?.status || '').trim().toLowerCase();
          let detailsPayload = null;
          let detailsText = '';
          if (payload?.details_json && typeof payload.details_json === 'object') {
            detailsPayload = payload.details_json;
          } else if (typeof payload?.details_json === 'string') {
            try {
              detailsPayload = JSON.parse(payload.details_json);
            } catch (_parseErr) {
              detailsPayload = null;
            }
          }
          if (detailsPayload && typeof detailsPayload === 'object') {
            const details = detailsPayload;
            const state = String(details.connection_state || '').trim();
            const desired = String(details.desired_state || '').trim();
            const mode = String(details.provider_mode || '').trim();
            const profile = String(details.vpn_profile_id || '').trim();
            const implemented = details.implemented === true ? 'yes' : 'no';
            detailsText = [
              state ? `state=${state}` : '',
              desired ? `desired=${desired}` : '',
              mode ? `mode=${mode}` : '',
              profile ? `profile=${profile}` : '',
              `implemented=${implemented}`,
            ].filter(Boolean).join(' ');
          }
          const vpnCommands = new Set(['SET_VPN_POLICY', 'SET_VPN_PROVIDER_PROFILE', 'VPN_CONNECT', 'VPN_DISCONNECT', 'VPN_STATUS_POLL']);
          if (detailsPayload && vpnCommands.has(command)) {
            const detailDeviceId = String(payload?.device_id || row?.device_id || state.selectedDeviceId || '').trim();
            if (detailDeviceId) {
              state.vpnStatus.byDevice[detailDeviceId] = {
                connection_state: String(detailsPayload.connection_state || '').trim() || 'unknown',
                desired_state: String(detailsPayload.desired_state || '').trim() || 'unknown',
                provider_mode: String(detailsPayload.provider_mode || '').trim() || 'unknown',
                vpn_profile_id: String(detailsPayload.vpn_profile_id || '').trim() || null,
                policy_configured: detailsPayload.policy_configured === true,
                updated_at_ms: Number(detailsPayload.updated_at_ms || 0),
                implemented: detailsPayload.implemented === true,
              };
              if (state.selectedDeviceId === detailDeviceId) {
                renderVpnStatus();
              }
            }
          }
          if (!commandId) return;
          if (eventType === 'mdm_failed' || status === 'failed') {
            markCommandFailed(commandId, (reason || (command ? `${command} failed` : 'failed')) + (detailsText ? ` | ${detailsText}` : ''));
          } else {
            markCommandExecuted(commandId, (command ? `${command} executed` : 'executed') + (detailsText ? ` | ${detailsText}` : ''));
          }
        });
    } catch (_err) {
      // Ignore intermittent polling errors; next tick can recover.
    }
  }

  function startAuthRefreshTimer() {
    if (state.telemetry.authRefreshTimer) {
      clearInterval(state.telemetry.authRefreshTimer);
    }
    state.telemetry.authRefreshTimer = setInterval(() => {
      refreshAuthToken().catch(() => {});
    }, AUTH_REFRESH_MS);
  }

  function startCommandAckPoller() {
    if (state.telemetry.commandAckTimer) {
      clearInterval(state.telemetry.commandAckTimer);
    }
    pollCommandAcks().catch(() => {});
    state.telemetry.commandAckTimer = setInterval(() => {
      pollCommandAcks().catch(() => {});
    }, COMMAND_ACK_POLL_MS);
  }

  async function refreshLiveStatusTelemetry() {
    if (state.telemetry.liveStatusRefreshInFlight) return;
    state.telemetry.liveStatusRefreshInFlight = true;
    try {
      await Promise.allSettled([
        loadDevices(),
        loadDashboardIntelligence(),
      ]);
    } finally {
      state.telemetry.liveStatusRefreshInFlight = false;
    }
  }

  function startLiveStatusAutoRefresh() {
    if (state.telemetry.liveStatusRefreshTimer) {
      clearInterval(state.telemetry.liveStatusRefreshTimer);
      state.telemetry.liveStatusRefreshTimer = null;
    }
    state.telemetry.liveStatusRefreshTimer = setInterval(() => {
      refreshLiveStatusTelemetry().catch(() => {});
    }, getQueueRefreshMs());
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

  function parseCapabilities(device) {
    if (!device) return {};
    if (device.capabilities && typeof device.capabilities === 'object') return device.capabilities;
    if (device.capabilities_json && typeof device.capabilities_json === 'string') {
      try {
        const parsed = JSON.parse(device.capabilities_json);
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
    const capabilityMap = parseCapabilities(device);
    const explicitLovense = capabilityMap.lovense_available;
    const explicitPavlok = capabilityMap.pavlok_available;
    const lovense = typeof explicitLovense === 'boolean' ? explicitLovense : hasToyCapability(device, 'lovense');
    const pavlok = typeof explicitPavlok === 'boolean' ? explicitPavlok : hasToyCapability(device, 'pavlok');
    const toyInfoKnown = JSON.stringify(parseToyInfo(device)).length > 2;
    const rootAvailable = capabilityMap.root_available === true;
    const accessibilityEnabled = capabilityMap.accessibility_enabled === true;
    const deviceAdminActive = capabilityMap.device_admin_active === true;
    return {
      online,
      lovense,
      pavlok,
      toyInfoKnown,
      rootAvailable,
      accessibilityEnabled,
      deviceAdminActive,
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
      { label: 'Root', on: caps.rootAvailable },
      { label: 'Accessibility', on: caps.accessibilityEnabled },
      { label: 'Device Admin', on: caps.deviceAdminActive },
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
      'hp2-screenctl-overlay-send-btn',
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
    setDisabledHint('hp2-macro-disabled-hint', caps.selected && caps.online ? '' : onlineGateReason);
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
    if (state.telemetry.authRefreshTimer) {
      clearInterval(state.telemetry.authRefreshTimer);
      state.telemetry.authRefreshTimer = null;
    }
    if (state.telemetry.commandAckTimer) {
      clearInterval(state.telemetry.commandAckTimer);
      state.telemetry.commandAckTimer = null;
    }
    if (state.telemetry.liveStatusRefreshTimer) {
      clearInterval(state.telemetry.liveStatusRefreshTimer);
      state.telemetry.liveStatusRefreshTimer = null;
    }
    state.telemetry.liveStatusRefreshInFlight = false;
    if (state.queue.autoRefreshTimer) {
      clearInterval(state.queue.autoRefreshTimer);
      state.queue.autoRefreshTimer = null;
    }
    if (state.aiWarden.autoRefreshTimer) {
      clearInterval(state.aiWarden.autoRefreshTimer);
      state.aiWarden.autoRefreshTimer = null;
    }
    state.aiWarden = {
      config: null,
      stats: null,
      reports: [],
      autoRefreshTimer: null,
    };
    document.body.classList.remove('hp2-authenticated');
    setVisible('hp2-app', false);
    setVisible('hp2-login', true);
    byId('hp2-login-error').textContent = message;
    state.role = null;
    byId('hp2-role').textContent = 'GUEST';
    clearJwt();
  }

  function showApp() {
    document.body.classList.add('hp2-authenticated');
    setVisible('hp2-login', false);
    setVisible('hp2-app', true);
    startAuthRefreshTimer();
    startCommandAckPoller();
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
    if (el) {
      el.textContent = message || '';
      el.classList.remove('hp2-inline-result-ok', 'hp2-inline-result-warn', 'hp2-inline-result-bad');
      if (message) {
        const lower = String(message).toLowerCase();
        if (lower.includes('failed') || lower.includes('error')) el.classList.add('hp2-inline-result-bad');
        else if (lower.includes('warning')) el.classList.add('hp2-inline-result-warn');
        else el.classList.add('hp2-inline-result-ok');
      }
    }
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
    if (state.aiWarden.autoRefreshTimer) {
      clearInterval(state.aiWarden.autoRefreshTimer);
      state.aiWarden.autoRefreshTimer = null;
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
    if (viewName === 'ai-warden') {
      loadAiWardenDashboard().catch(() => {});
      state.aiWarden.autoRefreshTimer = setInterval(() => {
        loadAiWardenDashboard().catch(() => {});
      }, getQueueRefreshMs());
    }
    if (viewName === 'public-use') {
      loadPublicUseSettings().catch(() => {});
    }
    if (viewName === 'commands') {
      setCommandSection(state.commands.activeSection || 'all');
    }
    if (viewName === 'settings') {
      loadPublicStatusSettingsForForm().catch(() => {});
    }
  }

  function setCommandSection(section) {
    const allowed = new Set(['all', 'live-control', 'device-admin', 'messaging', 'automation', 'history']);
    const normalized = allowed.has(String(section || '').trim()) ? String(section || '').trim() : 'all';
    state.commands.activeSection = normalized;

    document.querySelectorAll('[data-cmd-section-target]').forEach((button) => {
      const active = button.dataset.cmdSectionTarget === normalized;
      button.classList.toggle('hp2-segment-active', active);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });

    document.querySelectorAll('#hp2-view-commands .hp2-command-card[data-cmd-section]').forEach((card) => {
      const cardSection = String(card.dataset.cmdSection || '').trim();
      const show = normalized === 'all' || cardSection === normalized;
      card.hidden = !show;
    });
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

  function formatHeartRate(device) {
    if (!device) return '-';
    const bpm = Number(device.latest_heart_rate);
    if (!Number.isFinite(bpm) || bpm <= 0) return '-';
    return `${Math.round(bpm)} bpm`;
  }

  function renderEdgeStatusCard() {
    const stateEl = byId('hp2-dashboard-edge-state');
    const levelEl = byId('hp2-dashboard-edge-level');
    const thresholdsEl = byId('hp2-dashboard-edge-thresholds');
    const updatedEl = byId('hp2-dashboard-edge-updated');
    if (!stateEl || !levelEl || !thresholdsEl || !updatedEl) return;

    const d = selectedDevice();
    const edge = d?.hr_edge || null;
    if (!edge || typeof edge !== 'object') {
      stateEl.textContent = '-';
      levelEl.textContent = '-';
      thresholdsEl.textContent = '-';
      updatedEl.textContent = '-';
      return;
    }

    const edgeState = String(edge.state || 'idle').trim() || 'idle';
    const lastLevel = Number(edge.last_level);
    const pause = Number(edge.pause_bpm);
    const resume = Number(edge.resume_bpm);
    const updatedAt = String(edge.updated_at || '').trim();

    stateEl.textContent = edgeState.replaceAll('_', ' ');
    levelEl.textContent = Number.isFinite(lastLevel) && lastLevel >= 0 ? `${Math.trunc(lastLevel)}/20` : '-';
    thresholdsEl.textContent = Number.isFinite(resume) && Number.isFinite(pause)
      ? `${Math.trunc(resume)} to ${Math.trunc(pause)} bpm`
      : '-';
    updatedEl.textContent = updatedAt ? fmtDate(updatedAt) : '-';
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
    byId('hp2-detail-heart-rate').textContent = formatHeartRate(d);
    byId('hp2-detail-vitals-at').textContent = d?.latest_vitals_at ? fmtDate(d.latest_vitals_at) : '-';
    byId('hp2-detail-last').textContent = d ? fmtDate(d.last_seen) : '-';
    byId('hp2-dashboard-battery').textContent = d && Number.isFinite(Number(d.battery_pct)) ? `${d.battery_pct}%` : '-';
    byId('hp2-dashboard-heart-rate').textContent = formatHeartRate(d);
    byId('hp2-dashboard-connection').textContent = d ? (deviceOnline(d) ? 'Connected' : 'Offline') : '-';
    renderEdgeStatusCard();
    renderDashboardAlerts();
    renderLiveMap();
    applyCommandGating();
    applyAdminBulkDeviceControlsAccess();
    renderDeviceApps();
    renderVpnStatus();
  }

  function renderDeviceApps() {
    const listEl = byId('hp2-device-apps-list');
    const metaEl = byId('hp2-device-apps-meta');
    const statusEl = byId('hp2-device-apps-status');
    if (!listEl || !metaEl) return;

    const selectedId = state.selectedDeviceId;
    if (!selectedId) {
      metaEl.textContent = 'Select a device';
      if (statusEl) statusEl.textContent = 'No sync yet.';
      listEl.innerHTML = '<li class="hp2-muted">Select a device to load installed apps.</li>';
      refreshAppActionNameSuggestions();
      return;
    }

    const syncAt = state.appInventory.latestSyncAt ? fmtDate(state.appInventory.latestSyncAt) : 'never';
    const pollTag = state.appInventory.latestPollId ? ` poll=${state.appInventory.latestPollId}` : '';
    metaEl.textContent = `Device ${selectedId} | ${state.appInventory.apps.length} apps | last sync ${syncAt}${pollTag}`;
    if (statusEl) {
      const changed = Number(state.appInventory.latestChangedCount || 0);
      const source = String(state.appInventory.latestSource || 'unknown');
      statusEl.textContent = `Latest sync source=${source} changed=${changed}`;
    }

    const rows = Array.isArray(state.appInventory.apps) ? state.appInventory.apps : [];
    if (!rows.length) {
      listEl.innerHTML = '<li class="hp2-muted">No app inventory yet. Run Poll Installed Apps.</li>';
      refreshAppActionNameSuggestions();
      return;
    }

    listEl.innerHTML = rows.map((app) => {
      const label = String(app.app_label || app.package_name || 'Unknown');
      const pkg = String(app.package_name || '');
      const flags = [
        Number(app.is_system || 0) === 1 ? 'system' : 'user',
        Number(app.is_enabled || 0) === 1 ? 'enabled' : 'disabled',
        Number(app.is_suspended || 0) === 1 ? 'suspended' : null,
      ].filter(Boolean).join(' | ');
      const version = String(app.version_name || app.version_code || '').trim();
      return `<li>
        <div class="hp2-feed-item-row">
          <strong>${escapeHtml(label)}</strong>
          <span class="hp2-muted">${escapeHtml(flags)}</span>
        </div>
        <div class="hp2-meta-mono">${escapeHtml(pkg)}</div>
        <div class="hp2-muted">${escapeHtml(version ? `v${version}` : 'version n/a')}</div>
      </li>`;
    }).join('');

    refreshAppActionNameSuggestions();
  }

  function refreshAppActionNameSuggestions() {
    const datalist = byId('hp2-appctl-name-list');
    if (!datalist) return;
    const rows = Array.isArray(state.appInventory.apps) ? state.appInventory.apps : [];
    const values = new Set(['Instagram', 'TikTok', 'Chrome', 'Telegram', 'Discord']);
    rows.forEach((app) => {
      const label = String(app.app_label || '').trim();
      const pkg = String(app.package_name || '').trim();
      if (label) values.add(label);
      if (pkg) values.add(pkg);
    });
    datalist.innerHTML = Array.from(values)
      .sort((a, b) => a.localeCompare(b))
      .map((value) => `<option value="${escapeHtml(value)}"></option>`)
      .join('');
  }

  function renderVpnStatus() {
    const metaEl = byId('hp2-vpn-meta');
    const connectionEl = byId('hp2-vpn-connection');
    const desiredEl = byId('hp2-vpn-desired');
    const providerEl = byId('hp2-vpn-provider');
    const profileEl = byId('hp2-vpn-profile');
    const policyEl = byId('hp2-vpn-policy');
    const updatedEl = byId('hp2-vpn-updated');
    if (!metaEl || !connectionEl || !desiredEl || !providerEl || !profileEl || !policyEl || !updatedEl) return;

    const selectedId = state.selectedDeviceId;
    if (!selectedId) {
      metaEl.textContent = 'Select a device';
      connectionEl.textContent = '-';
      desiredEl.textContent = '-';
      providerEl.textContent = '-';
      profileEl.textContent = '-';
      policyEl.textContent = '-';
      updatedEl.textContent = '-';
      return;
    }

    const status = state.vpnStatus.byDevice[selectedId] || null;
    metaEl.textContent = `Device ${selectedId}`;
    if (!status) {
      connectionEl.textContent = 'unknown';
      desiredEl.textContent = 'unknown';
      providerEl.textContent = 'unknown';
      profileEl.textContent = 'unknown';
      policyEl.textContent = 'unknown';
      updatedEl.textContent = 'never';
      return;
    }

    connectionEl.textContent = String(status.connection_state || 'unknown');
    desiredEl.textContent = String(status.desired_state || 'unknown');
    providerEl.textContent = String(status.provider_mode || 'unknown');
    profileEl.textContent = String(status.vpn_profile_id || 'none');
    policyEl.textContent = status.policy_configured ? 'configured' : 'not configured';
    const updatedAtMs = Number(status.updated_at_ms || 0);
    updatedEl.textContent = updatedAtMs > 0 ? fmtDate(new Date(updatedAtMs).toISOString()) : 'unknown';
  }

  async function loadDeviceApps() {
    if (!state.selectedDeviceId) {
      state.appInventory.apps = [];
      state.appInventory.latestSyncAt = null;
      state.appInventory.latestPollId = null;
      state.appInventory.latestChangedCount = 0;
      state.appInventory.latestSource = null;
      renderDeviceApps();
      return;
    }

    const search = String(byId('hp2-device-apps-search')?.value || state.appInventory.query.search || '').trim();
    const scope = String(byId('hp2-device-apps-system-filter')?.value || (state.appInventory.query.includeSystem ? 'all' : 'user')).trim();
    const includeSystem = scope !== 'user';
    state.appInventory.query.search = search;
    state.appInventory.query.includeSystem = includeSystem;

    const params = new URLSearchParams();
    params.set('device_id', state.selectedDeviceId);
    params.set('limit', '500');
    params.set('sort', 'label');
    params.set('order', 'asc');
    if (!includeSystem) params.set('include_system', 'false');
    if (search) params.set('q', search);

    const payload = await apiGet(`/api/handler/device-apps?${params.toString()}`);
    state.appInventory.apps = Array.isArray(payload?.apps) ? payload.apps : [];
    state.appInventory.latestSyncId = Number(payload?.latest_sync?.id || 0);
    state.appInventory.latestSyncAt = payload?.latest_sync?.created_at || null;
    state.appInventory.latestPollId = payload?.latest_sync?.poll_id || null;
    state.appInventory.latestChangedCount = Number(payload?.latest_sync?.changed_count || 0);
    state.appInventory.latestSource = payload?.latest_sync?.source || null;
    renderDeviceApps();
  }

  async function pollDeviceApps() {
    if (!state.selectedDeviceId) {
      setInlineResult('hp2-device-apps-result', 'Select a device first.');
      return;
    }
    setInlineResult('hp2-device-apps-result', 'Sending app poll command...');
    try {
      const scope = String(byId('hp2-device-apps-system-filter')?.value || 'all').trim();
      await apiPost('/api/handler/device-apps/poll', {
        device_id: state.selectedDeviceId,
        include_system: scope !== 'user',
        full_snapshot: true,
      });
      setInlineResult('hp2-device-apps-result', 'Poll command sent. Refresh in a moment.');
      pushFeed(`App poll requested for ${state.selectedDeviceId}`);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult('hp2-device-apps-result', `Failed: ${err.message}`);
      }
    }
  }

  async function sendVpnStatusAction(action) {
    if (!state.selectedDeviceId) {
      setInlineResult('hp2-vpn-status-result', 'Select a device first.');
      return;
    }
    const title = action.replaceAll('_', ' ');
    await sendControlCommand({
      title,
      action,
      fields: {},
      resultId: 'hp2-vpn-status-result',
      confirmText: 'Send',
      message: `${title} for ${selectedDeviceLabel()}?`,
      historyDetail: action,
    });
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
      await loadDeviceApps();
    }
    state.telemetry.devicesAt = Date.now();
    renderFreshness();
    startLiveStatusAutoRefresh();
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

      if (msg.type === 'device_app_sync' && msg.device_id) {
        if (state.selectedDeviceId === msg.device_id) {
          state.appInventory.latestSyncId = Number(msg.sync_id || state.appInventory.latestSyncId || 0);
          state.appInventory.latestSyncAt = msg.updated_at || state.appInventory.latestSyncAt;
          state.appInventory.latestChangedCount = Number(msg.changed_count || 0);
          loadDeviceApps().catch(() => {});
        }
        pushFeed(`App sync received: ${msg.device_id}`);
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
      applyAdminBulkDeviceControlsAccess();
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
      loadPushSchema(),
      loadPublicStatusSettingsForForm(),
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

  function setDrawerResult(message) {
    setInlineResult('hp2-drawer-result', message || '');
  }

  function setAiWardenResult(message) {
    setInlineResult('hp2-ai-result', message || '');
  }

  function parseSelectBool(id, fallback = false) {
    const value = String(byId(id)?.value || '').trim().toLowerCase();
    if (value === 'true') return true;
    if (value === 'false') return false;
    return !!fallback;
  }

  function normalizeAiRulesText(text) {
    return String(text || '')
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean);
  }

  function renderAiWardenStats() {
    const host = byId('hp2-ai-stats-list');
    if (!host) return;
    const stats = state.aiWarden.stats;
    if (!stats || typeof stats !== 'object') {
      host.innerHTML = queueListEmpty('No stats loaded yet.');
      return;
    }
    const counts = stats.counts || {};
    const tunnel = stats.tunnel || {};
    const health = stats.remote_health || {};
    host.innerHTML = `
      <li>
        <div class="hp2-feed-item-row"><strong>Remote Health</strong><span class="${severityClass(health.ok ? 'info' : 'warning')}">${escapeHtml(health.ok ? 'ok' : 'down')}</span></div>
        <div class="hp2-muted">${escapeHtml(String(health.url || '-'))}</div>
        <div class="hp2-muted">status=${escapeHtml(String(health.status_code ?? '-'))} latency=${escapeHtml(String(health.latency_ms ?? '-'))}ms</div>
        ${health.error ? `<div class="hp2-muted">${escapeHtml(String(health.error))}</div>` : ''}
      </li>
      <li>
        <div class="hp2-feed-item-row"><strong>Tunnel</strong><span class="${severityClass(tunnel.connected ? 'info' : 'warning')}">${escapeHtml(tunnel.connected ? 'connected' : 'disconnected')}</span></div>
        <div class="hp2-muted">queue_depth=${escapeHtml(String(tunnel.queue_depth ?? 0))}</div>
      </li>
      <li>
        <div class="hp2-feed-item-row"><strong>Activity Window</strong><span class="${severityClass('info')}">${escapeHtml(String(stats.window_hours || 0))}h</span></div>
        <div class="hp2-muted">corrections=${escapeHtml(String(counts.corrections ?? 0))} behavior=${escapeHtml(String(counts.behavior_events ?? 0))}</div>
        <div class="hp2-muted">enforcement=${escapeHtml(String(counts.enforcement_events ?? 0))} social=${escapeHtml(String(counts.social_posts ?? 0))}</div>
        <div class="hp2-muted">reports=${escapeHtml(String(counts.reports_received ?? 0))} rules=${escapeHtml(String(stats.rules_count ?? 0))}</div>
      </li>
    `;
  }

  function renderAiWardenReports() {
    const host = byId('hp2-ai-reports-list');
    if (!host) return;
    const rows = Array.isArray(state.aiWarden.reports) ? state.aiWarden.reports : [];
    if (!rows.length) {
      host.innerHTML = queueListEmpty('No AI reports yet.');
      return;
    }
    host.innerHTML = rows.slice(0, 60).map((row) => {
      const reportType = String(row?.report_type || 'report');
      const summary = String(row?.summary || '').trim();
      const severity = String(row?.severity || 'info').toLowerCase();
      const source = String(row?.source || 'remote_ai');
      return `<li>
        <div class="hp2-feed-item-row"><strong>${escapeHtml(reportType)}</strong><span class="${severityClass(severity === 'critical' || severity === 'high' ? 'warning' : 'info')}">${escapeHtml(severity)}</span></div>
        ${summary ? `<div class="hp2-muted">${escapeHtml(summary)}</div>` : ''}
        <div class="hp2-muted">${escapeHtml(source)} • ${escapeHtml(fmtDate(row?.created_at))}</div>
      </li>`;
    }).join('');
  }

  function applyAiWardenConfig(config) {
    if (!config || typeof config !== 'object') return;
    state.aiWarden.config = config;
    if (byId('hp2-ai-enabled')) byId('hp2-ai-enabled').value = config.enabled ? 'true' : 'false';
    if (byId('hp2-ai-name')) byId('hp2-ai-name').value = String(config.ai_name || '');
    if (byId('hp2-ai-provider')) byId('hp2-ai-provider').value = String(config.provider || '');
    if (byId('hp2-ai-server-base-url')) byId('hp2-ai-server-base-url').value = String(config.server_base_url || '');
    if (byId('hp2-ai-info')) byId('hp2-ai-info').value = String(config.info || '');
    if (byId('hp2-ai-rules')) byId('hp2-ai-rules').value = Array.isArray(config.rules) ? config.rules.join('\n') : '';
    if (byId('hp2-ai-auto-enforce')) byId('hp2-ai-auto-enforce').value = config.auto_enforce ? 'true' : 'false';
    if (byId('hp2-ai-auto-social')) byId('hp2-ai-auto-social').value = config.auto_social_posting ? 'true' : 'false';
    if (byId('hp2-ai-clear-api-key')) byId('hp2-ai-clear-api-key').value = 'false';
    if (byId('hp2-ai-api-key')) byId('hp2-ai-api-key').value = '';
    const note = [];
    if (config.has_api_key) {
      note.push(`api_key=${String(config.api_key_masked || 'configured')}`);
    } else {
      note.push('api_key=not configured');
    }
    if (config.ws_endpoint_url) note.push(`ws=${String(config.ws_endpoint_url)}`);
    if (config.ingress_secret_source) note.push(`auth=${String(config.ingress_secret_source)}`);
    setInlineResult('hp2-ai-config-note', note.join(' • '));
  }

  async function loadAiWardenDashboard() {
    const statsHost = byId('hp2-ai-stats-list');
    const reportsHost = byId('hp2-ai-reports-list');
    if (statsHost) statsHost.innerHTML = queueListEmpty('Loading AI stats...');
    if (reportsHost) reportsHost.innerHTML = queueListEmpty('Loading AI reports...');
    try {
      let configError = null;
      let statsError = null;
      let reportsError = null;
      const [config, stats, reportsPayload] = await Promise.all([
        apiGet('/api/handler/ai-warden/config').catch((err) => {
          configError = err;
          return null;
        }),
        apiGet('/api/handler/ai-warden/stats?window_hours=24').catch((err) => {
          statsError = err;
          return null;
        }),
        apiGet('/api/handler/ai-warden/reports?limit=60').catch((err) => {
          reportsError = err;
          return { reports: [] };
        }),
      ]);

      if (config) applyAiWardenConfig(config);
      state.aiWarden.stats = stats || null;
      state.aiWarden.reports = Array.isArray(reportsPayload?.reports) ? reportsPayload.reports : [];
      renderAiWardenStats();
      renderAiWardenReports();
      state.telemetry.aiWardenAt = Date.now();

      const errors = [configError, statsError, reportsError]
        .filter(Boolean)
        .map((err) => String(err?.message || err));
      if (errors.length) {
        setAiWardenResult(`Partial load issue: ${errors.join(' | ')}`);
      } else {
        setAiWardenResult('');
      }
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        if (statsHost) statsHost.innerHTML = queueListEmpty(`Failed to load AI stats: ${escapeHtml(err.message)}`);
        if (reportsHost) reportsHost.innerHTML = queueListEmpty('Failed to load AI reports.');
      }
    }
  }

  async function saveAiWardenConfig() {
    setAiWardenResult('Saving AI Warden config...');
    const payload = {
      enabled: parseSelectBool('hp2-ai-enabled', false),
      ai_name: String(byId('hp2-ai-name')?.value || '').trim(),
      provider: String(byId('hp2-ai-provider')?.value || '').trim(),
      server_base_url: String(byId('hp2-ai-server-base-url')?.value || '').trim(),
      info: String(byId('hp2-ai-info')?.value || '').trim(),
      rules: normalizeAiRulesText(byId('hp2-ai-rules')?.value || ''),
      auto_enforce: parseSelectBool('hp2-ai-auto-enforce', false),
      auto_social_posting: parseSelectBool('hp2-ai-auto-social', false),
      clear_api_key: parseSelectBool('hp2-ai-clear-api-key', false),
    };
    const apiKey = String(byId('hp2-ai-api-key')?.value || '').trim();
    if (apiKey) payload.api_key = apiKey;
    try {
      const config = await apiPost('/api/handler/ai-warden/config', payload);
      applyAiWardenConfig(config);
      setAiWardenResult('AI Warden config saved.');
      await loadAiWardenDashboard();
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setAiWardenResult(`Failed to save AI config: ${err.message}`);
      }
    }
  }

  function setPublicUseResult(message) {
    setInlineResult('hp2-public-use-result', message || '');
  }

  function parseCsvHosts(value) {
    return String(value || '')
      .split(',')
      .map((item) => item.trim().toLowerCase())
      .filter(Boolean);
  }

  function selectedMultiValues(id) {
    const select = byId(id);
    if (!select) return [];
    return Array.from(select.selectedOptions || []).map((opt) => String(opt.value || '').trim()).filter(Boolean);
  }

  function parseProfilesJson(value) {
    const raw = String(value || '').trim();
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  }

  function renderPublicUseAnalytics(data) {
    const analyticsHost = byId('hp2-public-use-analytics');
    const blockedHost = byId('hp2-public-use-blocked');
    if (!analyticsHost || !blockedHost) return;

    const actions = Array.isArray(data?.actions) ? data.actions : [];
    const outcomes = Array.isArray(data?.outcomes) ? data.outcomes : [];
    const blocked = Array.isArray(data?.recent_blocked) ? data.recent_blocked : [];

    const lines = [
      `<li><strong>Events:</strong> ${escapeHtml(String(data?.event_count || 0))}</li>`,
      `<li><strong>Top Actions:</strong> ${escapeHtml(actions.slice(0, 5).map((r) => `${r.action}:${r.count}`).join(' | ') || 'none')}</li>`,
      `<li><strong>Outcomes:</strong> ${escapeHtml(outcomes.slice(0, 5).map((r) => `${r.outcome}:${r.count}`).join(' | ') || 'none')}</li>`,
    ];
    analyticsHost.innerHTML = lines.join('');

    blockedHost.innerHTML = blocked.length
      ? blocked.slice(0, 20).map((row) => `<li><strong>${escapeHtml(String(row.outcome || 'blocked'))}</strong> ${escapeHtml(String(row.action || 'unknown'))}<div class="hp2-muted">${escapeHtml(String(row.detail || ''))} • ${escapeHtml(fmtDate(row.created_at))}</div></li>`).join('')
      : queueListEmpty('No blocked events in this window.');
  }

  async function loadPublicUseAnalytics() {
    const analyticsHost = byId('hp2-public-use-analytics');
    const blockedHost = byId('hp2-public-use-blocked');
    if (analyticsHost) analyticsHost.innerHTML = queueListEmpty('Loading guest analytics...');
    if (blockedHost) blockedHost.innerHTML = queueListEmpty('Loading blocked events...');
    try {
      const data = await apiGet('/api/handler/public-use-analytics?hours=24');
      renderPublicUseAnalytics(data || {});
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        if (analyticsHost) analyticsHost.innerHTML = queueListEmpty(`Analytics unavailable: ${escapeHtml(err.message)}`);
      }
    }
  }

  async function setPublicUsePanic(minutes) {
    const mins = Math.max(0, Number(minutes || 0));
    setPublicUseResult(mins > 0 ? `Setting panic for ${mins}m...` : 'Clearing panic mode...');
    try {
      const response = await apiPost('/api/handler/public-use-panic', { minutes: mins });
      const panicUntil = String(response?.panic_until || '');
      setPublicUseResult(response?.panic_active ? `Panic active until ${fmtDate(panicUntil)}.` : 'Panic cleared.');
      await loadPublicUseSettings();
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setPublicUseResult(`Failed to update panic mode: ${err.message}`);
      }
    }
  }

  function applyPublicUseSettings(config) {
    if (!config || typeof config !== 'object') return;
    state.publicUse.config = config;

    if (byId('hp2-public-site-enabled')) byId('hp2-public-site-enabled').value = config.public_site_enabled ? 'true' : 'false';
    if (byId('hp2-public-use-enabled')) byId('hp2-public-use-enabled').value = config.guest_enabled ? 'true' : 'false';
    if (byId('hp2-public-use-device-id')) byId('hp2-public-use-device-id').value = String(config.guest_device_id || '');
    if (byId('hp2-public-use-show-location')) byId('hp2-public-use-show-location').value = config.guest_show_location ? 'true' : 'false';
    if (byId('hp2-public-use-location-precision')) byId('hp2-public-use-location-precision').value = String(config.guest_location_precision || 'approx');
    if (byId('hp2-public-use-lovense-live')) byId('hp2-public-use-lovense-live').value = config.guest_allow_lovense_live ? 'true' : 'false';
    if (byId('hp2-public-use-lovense-pulse')) byId('hp2-public-use-lovense-pulse').value = config.guest_allow_lovense_pulse ? 'true' : 'false';
    if (byId('hp2-public-use-pavlok-enabled')) byId('hp2-public-use-pavlok-enabled').value = config.guest_allow_pavlok ? 'true' : 'false';
    if (byId('hp2-public-use-pavlok-max-intensity')) {
      byId('hp2-public-use-pavlok-max-intensity').value = String(config.guest_pavlok_max_intensity || 60);
    }
    if (byId('hp2-public-use-open-url-enabled')) byId('hp2-public-use-open-url-enabled').value = config.guest_allow_open_url ? 'true' : 'false';
    if (byId('hp2-public-use-url-hosts')) {
      byId('hp2-public-use-url-hosts').value = Array.isArray(config.guest_allowed_url_hosts)
        ? config.guest_allowed_url_hosts.join(', ')
        : '';
    }

    const controlSelect = byId('hp2-public-use-phone-controls');
    if (controlSelect) {
      const options = Array.isArray(config.phone_control_options) ? config.phone_control_options : [];
      const selected = new Set(Array.isArray(config.guest_phone_controls) ? config.guest_phone_controls : []);
      controlSelect.innerHTML = options
        .map((action) => `<option value="${escapeHtml(action)}" ${selected.has(action) ? 'selected' : ''}>${escapeHtml(action)}</option>`)
        .join('');
    }

    if (byId('hp2-public-use-rate-per-min')) {
      byId('hp2-public-use-rate-per-min').value = String(config.guest_rate_limit_per_min || 18);
    }
    if (byId('hp2-public-use-rate-action-per-min')) {
      byId('hp2-public-use-rate-action-per-min').value = String(config.guest_rate_limit_per_action_per_min || 6);
    }
    if (byId('hp2-public-use-session-ttl-sec')) {
      byId('hp2-public-use-session-ttl-sec').value = String(config.guest_session_ttl_sec || 900);
    }
    if (byId('hp2-public-use-schedule-timezone')) {
      byId('hp2-public-use-schedule-timezone').value = String(config.guest_schedule_timezone || 'utc');
    }
    if (byId('hp2-public-use-schedule-profiles')) {
      const profiles = Array.isArray(config.guest_schedule_profiles) ? config.guest_schedule_profiles : [];
      byId('hp2-public-use-schedule-profiles').value = profiles.length ? JSON.stringify(profiles, null, 2) : '';
    }

    const panicActive = !!config.guest_panic_active;
    const panicUntil = String(config.guest_panic_until || '').trim();
    if (panicActive && panicUntil) {
      setPublicUseResult(`Panic active until ${fmtDate(panicUntil)}.`);
    }
  }

  async function loadPublicUseSettings() {
    setPublicUseResult('Loading Public Use settings...');
    try {
      const config = await apiGet('/api/handler/public-use-settings');
      applyPublicUseSettings(config);
      await loadPublicUseAnalytics();
      if (!config?.guest_panic_active) {
        setPublicUseResult('');
      }
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setPublicUseResult(`Failed to load Public Use settings: ${err.message}`);
      }
    }
  }

  async function savePublicUseSettings() {
    setPublicUseResult('Saving Public Use settings...');
    let scheduleProfiles = [];
    try {
      scheduleProfiles = parseProfilesJson(byId('hp2-public-use-schedule-profiles')?.value || '');
    } catch (_err) {
      setPublicUseResult('Schedule profiles must be valid JSON array.');
      return;
    }
    const payload = {
      public_site_enabled: parseSelectBool('hp2-public-site-enabled', true),
      guest_enabled: parseSelectBool('hp2-public-use-enabled', false),
      guest_device_id: String(byId('hp2-public-use-device-id')?.value || '').trim(),
      guest_show_location: parseSelectBool('hp2-public-use-show-location', true),
      guest_location_precision: String(byId('hp2-public-use-location-precision')?.value || 'approx').trim(),
      guest_allow_lovense_live: parseSelectBool('hp2-public-use-lovense-live', true),
      guest_allow_lovense_pulse: parseSelectBool('hp2-public-use-lovense-pulse', false),
      guest_allow_pavlok: parseSelectBool('hp2-public-use-pavlok-enabled', false),
      guest_pavlok_max_intensity: Number(byId('hp2-public-use-pavlok-max-intensity')?.value || 60),
      guest_phone_controls: selectedMultiValues('hp2-public-use-phone-controls'),
      guest_allow_open_url: parseSelectBool('hp2-public-use-open-url-enabled', false),
      guest_allowed_url_hosts: parseCsvHosts(byId('hp2-public-use-url-hosts')?.value || ''),
      guest_rate_limit_per_min: Number(byId('hp2-public-use-rate-per-min')?.value || 18),
      guest_rate_limit_per_action_per_min: Number(byId('hp2-public-use-rate-action-per-min')?.value || 6),
      guest_session_ttl_sec: Number(byId('hp2-public-use-session-ttl-sec')?.value || 900),
      guest_schedule_timezone: String(byId('hp2-public-use-schedule-timezone')?.value || 'utc').trim(),
      guest_schedule_profiles: scheduleProfiles,
    };
    try {
      const response = await apiPost('/api/handler/public-use-settings', payload);
      applyPublicUseSettings(response?.settings || payload);
      setPublicUseResult('Public Use settings saved.');
      await loadPublicUseAnalytics();
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setPublicUseResult(`Failed to save Public Use settings: ${err.message}`);
      }
    }
  }

  function normalizeDrawerMediaFilter(value) {
    const normalized = String(value || '').trim().toLowerCase();
    if (['all', 'media', 'image', 'video', 'audio', 'file'].includes(normalized)) {
      return normalized;
    }
    return 'all';
  }

  function syncDrawerMediaFilterControls() {
    const active = normalizeDrawerMediaFilter(state.drawer.mediaFilter);
    document.querySelectorAll('[data-drawer-media-filter]').forEach((button) => {
      const selected = normalizeDrawerMediaFilter(button.dataset.drawerMediaFilter) === active;
      button.classList.toggle('hp2-media-filter-btn-active', selected);
      button.setAttribute('aria-pressed', selected ? 'true' : 'false');
    });
  }

  function setDrawerMediaFilter(value) {
    state.drawer.mediaFilter = normalizeDrawerMediaFilter(value);
    syncDrawerMediaFilterControls();
    if (byId('hp2-view-drawer')?.classList.contains('hp2-view-active')) {
      loadEvidenceDrawer().catch(() => {});
    }
  }

  function isDrawerLightboxOpen() {
    return !byId('hp2-drawer-lightbox')?.classList.contains('hp2-hidden');
  }

  function closeDrawerLightbox() {
    const wrap = byId('hp2-drawer-lightbox');
    const img = byId('hp2-drawer-lightbox-image');
    const link = byId('hp2-drawer-lightbox-open');
    const prev = byId('hp2-drawer-lightbox-prev');
    const next = byId('hp2-drawer-lightbox-next');
    if (!wrap || !img || !link) return;
    wrap.classList.add('hp2-hidden');
    img.removeAttribute('src');
    img.alt = 'Drawer media';
    link.href = '#';
    if (prev) prev.disabled = true;
    if (next) next.disabled = true;
    state.drawer.lightboxGallery = [];
    state.drawer.lightboxIndex = -1;
  }

  function syncDrawerLightboxNavButtons() {
    const prev = byId('hp2-drawer-lightbox-prev');
    const next = byId('hp2-drawer-lightbox-next');
    const total = state.drawer.lightboxGallery.length;
    const idx = state.drawer.lightboxIndex;
    if (prev) prev.disabled = !(total > 1 && idx > 0);
    if (next) next.disabled = !(total > 1 && idx >= 0 && idx < total - 1);
  }

  function showDrawerLightboxAt(index) {
    const wrap = byId('hp2-drawer-lightbox');
    const img = byId('hp2-drawer-lightbox-image');
    const link = byId('hp2-drawer-lightbox-open');
    const gallery = state.drawer.lightboxGallery;
    if (!wrap || !img || !link || !gallery.length) return;
    const nextIndex = Math.max(0, Math.min(gallery.length - 1, Number(index || 0)));
    const item = gallery[nextIndex];
    if (!item || !item.src) return;
    state.drawer.lightboxIndex = nextIndex;
    img.src = item.src;
    img.alt = item.label || 'Drawer media';
    link.href = item.src;
    syncDrawerLightboxNavButtons();
    wrap.classList.remove('hp2-hidden');
  }

  function collectDrawerLightboxGallery() {
    const nodes = Array.from(document.querySelectorAll('#hp2-view-drawer [data-drawer-lightbox-src]'));
    return nodes.map((node) => ({
      src: String(node.dataset.drawerLightboxSrc || '').trim(),
      label: String(node.dataset.drawerLightboxLabel || 'Drawer media').trim(),
    })).filter((item) => item.src);
  }

  function openDrawerLightbox(src, label, options = {}) {
    const gallery = Array.isArray(options.gallery) && options.gallery.length
      ? options.gallery
      : collectDrawerLightboxGallery();
    state.drawer.lightboxGallery = gallery;
    let index = Number(options.index);
    if (!Number.isInteger(index) || index < 0 || index >= gallery.length) {
      index = Math.max(0, gallery.findIndex((item) => item.src === src));
    }
    if (!gallery.length) {
      state.drawer.lightboxGallery = [{ src, label: label || 'Drawer media' }];
      index = 0;
    }
    showDrawerLightboxAt(index);
  }

  function navigateDrawerLightbox(delta) {
    if (!isDrawerLightboxOpen()) return;
    const total = state.drawer.lightboxGallery.length;
    if (!total) return;
    showDrawerLightboxAt(state.drawer.lightboxIndex + delta);
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

  function escalationTone(level, status) {
    const safeLevel = String(level || '').toLowerCase();
    const safeStatus = String(status || '').toLowerCase();
    if (safeStatus === 'resolved') return 'info';
    if (safeLevel === 'catastrophic' || safeLevel === 'severe') return 'warning';
    return 'info';
  }

  async function loadQueueEscalations() {
    const listEl = byId('hp2-queue-escalation-list');
    const filter = byId('hp2-queue-escalation-filter')?.value || 'all';
    listEl.innerHTML = queueListEmpty('Loading escalation queue...');
    try {
      const items = await apiGet(`/api/handler/shame/escalations?status=${encodeURIComponent(filter)}&limit=200`);
      let rows = Array.isArray(items) ? items : [];
      rows.sort((a, b) => compareByTimeDescOrAsc(a.created_at, b.created_at));
      if (!rows.length) {
        listEl.innerHTML = queueListEmpty('No escalations in this filter.');
        return;
      }
      listEl.innerHTML = rows.map((item) => {
        const id = Number(item.id || 0);
        const level = String(item.level || 'high');
        const status = String(item.status || 'pending');
        const canResolve = status !== 'resolved';
        const canActivate = status === 'pending' && isAdminRole();
        const actionHint = String(item.action_hint || '').replaceAll('_', ' ');
        const note = String(item.note || '').trim();
        const resolvedAt = String(item.resolved_at || '').trim();
        return `<li>
          <div class="hp2-feed-item-row">
            <strong>#${id} ${escapeHtml(level)}</strong>
            <span class="${severityClass(escalationTone(level, status))}">${escapeHtml(status)}</span>
          </div>
          <div class="hp2-muted">Trigger score: ${escapeHtml(String(item.trigger_score ?? 0))} • Action: ${escapeHtml(actionHint || 'none')}</div>
          ${note ? `<div class="hp2-muted">${escapeHtml(note)}</div>` : ''}
          <div class="hp2-muted">Created: ${escapeHtml(fmtDate(item.created_at))}${resolvedAt ? ` • Resolved: ${escapeHtml(fmtDate(resolvedAt))}` : ''}</div>
          ${canResolve ? `<div class="hp2-queue-actions">${canActivate ? `<button type="button" class="hp2-btn hp2-btn-primary" data-q-action="escalation-activate" data-id="${id}">Activate</button>` : ''}<button type="button" class="hp2-btn hp2-btn-ghost" data-q-action="escalation-resolve" data-id="${id}">Resolve</button></div>` : ''}
        </li>`;
      }).join('');
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        listEl.innerHTML = queueListEmpty('Failed to load escalation queue.');
      }
    }
  }

  async function resolveQueueEscalation(escalationId) {
    const id = Number(escalationId || 0);
    if (!id) return;
    const response = await askInlineText({
      title: 'Resolve Escalation',
      message: 'Optional note for resolution log.',
      inputLabel: 'Resolution note',
      inputValue: '',
      multiline: true,
      allowEmpty: true,
      confirmText: 'Resolve',
    });
    if (!response.confirmed) return;
    setQueueResult('Resolving escalation...');
    try {
      await apiPost(`/api/handler/shame/escalations/${encodeURIComponent(String(id))}/resolve`, {
        note: response.value || '',
      });
      setQueueResult(`Escalation #${id} resolved.`);
      await loadQueueEscalations();
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setQueueResult(`Failed to resolve escalation: ${err.message}`);
      }
    }
  }

  async function activateQueueEscalation(escalationId) {
    const id = Number(escalationId || 0);
    if (!id) return;
    if (!isAdminRole()) {
      setQueueResult('Only admins can activate escalations.');
      return;
    }
    const response = await askInlineText({
      title: 'Activate Escalation',
      message: 'Optional note for why this escalation is being forced active now.',
      inputLabel: 'Activation note',
      inputValue: '',
      multiline: true,
      allowEmpty: true,
      confirmText: 'Activate',
    });
    if (!response.confirmed) return;
    setQueueResult('Activating escalation...');
    try {
      await apiPost(`/api/handler/shame/escalations/${encodeURIComponent(String(id))}/activate`, {
        note: response.value || '',
      });
      setQueueResult(`Escalation #${id} set active.`);
      await loadQueueEscalations();
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setQueueResult(`Failed to activate escalation: ${err.message}`);
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

  function inferDrawerAttachmentType(entry) {
    const kind = String(entry?.media_kind || entry?.kind || '').trim().toLowerCase();
    const metadataType = String(entry?.metadata?.content_type || '').trim().toLowerCase();
    const url = String(entry?.url || '').trim().toLowerCase();
    if (kind === 'image' || metadataType.startsWith('image/') || /\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(url)) return 'image';
    if (kind === 'video' || metadataType.startsWith('video/') || /\.(mp4|webm|mov|m4v|avi|mkv)$/i.test(url)) return 'video';
    if (kind === 'audio' || metadataType.startsWith('audio/') || /\.(mp3|wav|ogg|m4a|aac|flac)$/i.test(url)) return 'audio';
    return 'file';
  }

  function itemMatchesDrawerMediaFilter(attachments) {
    const mode = normalizeDrawerMediaFilter(state.drawer.mediaFilter);
    if (mode === 'all') return true;
    const rows = Array.isArray(attachments) ? attachments : [];
    if (!rows.length) return false;
    const types = rows.map((entry) => inferDrawerAttachmentType(entry));
    if (mode === 'media') return types.length > 0;
    return types.includes(mode);
  }

  function renderDrawerAttachmentLinks(attachments) {
    const rows = Array.isArray(attachments) ? attachments : [];
    if (!rows.length) return '';
    const rendered = rows.map((entry) => {
      const mediaType = inferDrawerAttachmentType(entry);
      const label = String(entry?.label || mediaType || 'media').trim() || 'media';
      const url = String(entry?.url || '').trim();
      if (!url) return '';
      const safeUrl = escapeHtml(url);
      if (mediaType === 'image') {
        return `<div class="hp2-drawer-attachment"><button type="button" class="hp2-drawer-lightbox-trigger" data-drawer-lightbox-src="${safeUrl}" data-drawer-lightbox-label="${escapeHtml(label)}" aria-label="Open image preview"><img class="hp2-drawer-media-preview hp2-drawer-media-image" src="${safeUrl}" alt="${escapeHtml(label)}" loading="lazy" /></button><a class="hp2-drawer-media-label" href="${safeUrl}" target="_blank" rel="noopener">${escapeHtml(label)}</a></div>`;
      }
      if (mediaType === 'video') {
        return `<div class="hp2-drawer-attachment"><video class="hp2-drawer-media-preview hp2-drawer-media-video" src="${safeUrl}" controls preload="metadata"></video><a class="hp2-drawer-media-label" href="${safeUrl}" target="_blank" rel="noopener">${escapeHtml(label)}</a></div>`;
      }
      if (mediaType === 'audio') {
        return `<div class="hp2-drawer-attachment"><audio class="hp2-drawer-media-preview hp2-drawer-media-audio" src="${safeUrl}" controls preload="metadata"></audio><a class="hp2-drawer-media-label" href="${safeUrl}" target="_blank" rel="noopener">${escapeHtml(label)}</a></div>`;
      }
      return `<div class="hp2-drawer-attachment"><a class="hp2-drawer-media-label" href="${safeUrl}" target="_blank" rel="noopener">${escapeHtml(label)}</a></div>`;
    }).filter(Boolean);
    if (!rendered.length) return '';
    return `<div class="hp2-drawer-attachments">${rendered.join('')}</div>`;
  }

  async function uploadDrawerMedia(file) {
    const form = new FormData();
    form.append('file', file);
    const response = await apiFetch('/api/handler/drawer/upload', {
      method: 'POST',
      body: form,
    });
    return response.json().catch(() => ({}));
  }

  async function createDrawerLimboItem() {
    const prompt = String(byId('hp2-drawer-limbo-new-text')?.value || '').trim();
    const mediaFile = byId('hp2-drawer-limbo-file')?.files?.[0] || null;
    if (!prompt) {
      setDrawerResult('Limbo prompt is required.');
      return;
    }
    setDrawerResult('Creating limbo item...');
    try {
      const created = await apiPost('/api/handler/limbo', {
        prompt_text: prompt,
        source: 'handler',
      });
      const limboId = Number(created?.id || 0);
      if (mediaFile && limboId > 0) {
        const upload = await uploadDrawerMedia(mediaFile);
        await apiPost(`/api/handler/limbo/${encodeURIComponent(String(limboId))}/attachments`, {
          media_kind: String(upload?.media_kind || 'file').trim().toLowerCase(),
          label: mediaFile.name,
          url: String(upload?.url || '').trim(),
          metadata: {
            content_type: mediaFile.type || '',
            size_bytes: mediaFile.size || 0,
            filename: mediaFile.name,
          },
        });
      }
      if (byId('hp2-drawer-limbo-new-text')) byId('hp2-drawer-limbo-new-text').value = '';
      if (byId('hp2-drawer-limbo-file')) byId('hp2-drawer-limbo-file').value = '';
      setDrawerResult('Limbo item logged.');
      await loadEvidenceDrawer();
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setDrawerResult(`Failed to create limbo item: ${err.message}`);
      }
    }
  }

  async function createDrawerEvidenceItem() {
    const title = String(byId('hp2-drawer-evidence-title')?.value || '').trim();
    const summary = String(byId('hp2-drawer-evidence-summary')?.value || '').trim();
    const category = String(byId('hp2-drawer-evidence-category')?.value || 'consequence').trim().toLowerCase();
    const severity = String(byId('hp2-drawer-evidence-severity')?.value || 'medium').trim().toLowerCase();
    const mediaFile = byId('hp2-drawer-evidence-file')?.files?.[0] || null;
    if (!title) {
      setDrawerResult('Evidence title is required.');
      return;
    }
    setDrawerResult('Creating evidence log...');
    try {
      const created = await apiPost('/api/handler/tpe/evidence', {
        device_id: state.selectedDeviceId || null,
        category,
        severity,
        title,
        summary: summary || null,
        metadata: {
          source: 'handler_v2_drawer',
        },
      });
      const evidenceId = Number(created?.id || 0);
      if (mediaFile && evidenceId > 0) {
        const upload = await uploadDrawerMedia(mediaFile);
        await apiPost(`/api/handler/tpe/evidence/${encodeURIComponent(String(evidenceId))}/attachments`, {
          kind: String(upload?.media_kind || 'file').trim().toLowerCase(),
          label: mediaFile.name,
          url: String(upload?.url || '').trim(),
          metadata: {
            content_type: mediaFile.type || '',
            size_bytes: mediaFile.size || 0,
            filename: mediaFile.name,
          },
        });
      }
      if (byId('hp2-drawer-evidence-title')) byId('hp2-drawer-evidence-title').value = '';
      if (byId('hp2-drawer-evidence-summary')) byId('hp2-drawer-evidence-summary').value = '';
      if (byId('hp2-drawer-evidence-file')) byId('hp2-drawer-evidence-file').value = '';
      setDrawerResult('Evidence log created.');
      await loadEvidenceDrawer();
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setDrawerResult(`Failed to create evidence log: ${err.message}`);
      }
    }
  }

  async function loadEvidenceDrawer() {
    const pendingEl = byId('hp2-drawer-limbo-pending');
    const resolvedEl = byId('hp2-drawer-limbo-resolved');
    const evidenceEl = byId('hp2-drawer-evidence-list');
    const correctionsEl = byId('hp2-drawer-corrections');
    pendingEl.innerHTML = queueListEmpty('Loading pending limbo items...');
    resolvedEl.innerHTML = queueListEmpty('Loading resolved limbo items...');
    if (evidenceEl) evidenceEl.innerHTML = queueListEmpty('Loading evidence logs...');
    if (correctionsEl) correctionsEl.innerHTML = queueListEmpty('Loading correction events...');
    try {
      const [pendingRows, allRows, evidenceRows, correctionRows] = await Promise.all([
        apiGet('/api/handler/limbo?status=pending&limit=200').catch(() => []),
        apiGet('/api/handler/limbo?status=all&limit=300').catch(() => []),
        apiGet(`/api/handler/tpe/evidence?limit=120${state.selectedDeviceId ? `&device_id=${encodeURIComponent(state.selectedDeviceId)}` : ''}`).catch(() => []),
        apiGet('/api/handler/drawer/corrections?limit=150').catch(() => []),
      ]);
      let pending = Array.isArray(pendingRows) ? pendingRows : [];
      const all = Array.isArray(allRows) ? allRows : [];
      let resolved = all.filter((item) => item.status !== 'pending');
      const evidence = Array.isArray(evidenceRows) ? evidenceRows : [];
      const corrections = Array.isArray(correctionRows) ? correctionRows : [];

      pending = pending.filter((item) => includeBySharedFilter(item.status || 'pending', 'open-only'));
      resolved = resolved.filter((item) => includeBySharedFilter(item.status || 'resolved', 'resolved-only'));
      pending = pending.filter((item) => itemMatchesDrawerMediaFilter(item?.attachments));
      resolved = resolved.filter((item) => itemMatchesDrawerMediaFilter(item?.attachments));
      const visibleEvidence = evidence.filter((item) => itemMatchesDrawerMediaFilter(item?.attachments));

      pending.sort((a, b) => compareByTimeDescOrAsc(a.created_at, b.created_at));
      resolved.sort((a, b) => compareByTimeDescOrAsc(a.created_at, b.created_at));

      pendingEl.innerHTML = pending.length ? pending.map((item) => {
        const id = Number(item.id || 0);
        const attachmentCount = Array.isArray(item.attachments) ? item.attachments.length : 0;
        return `<li>
          <div class="hp2-feed-item-row">
            <strong>${escapeHtml(item.prompt_text || '')}</strong>
            <span class="${severityClass('warning')}">${escapeHtml(item.source || 'handler')}</span>
          </div>
          <div class="hp2-muted">${escapeHtml(fmtDate(item.created_at))}</div>
          ${attachmentCount ? `<div class="hp2-muted">Media: ${attachmentCount}</div>` : ''}
          ${renderDrawerAttachmentLinks(item.attachments)}
          <div class="hp2-queue-actions">
            <button type="button" class="hp2-btn hp2-btn-ghost" data-q-action="limbo-answer" data-id="${id}">Answer</button>
            <button type="button" class="hp2-btn hp2-btn-ghost" data-q-action="limbo-dismiss" data-id="${id}">Dismiss</button>
          </div>
        </li>`;
      }).join('') : queueListEmpty('No pending limbo items.');

      resolvedEl.innerHTML = resolved.length ? resolved.slice(0, 120).map((item) => {
        const id = Number(item.id || 0);
        const resolution = item.status === 'answered' ? (item.answer_text || 'Answered') : (item.dismissed_reason || 'Dismissed');
        const attachmentCount = Array.isArray(item.attachments) ? item.attachments.length : 0;
        return `<li>
          <div class="hp2-feed-item-row">
            <strong>${escapeHtml(item.prompt_text || '')}</strong>
            <span class="${severityClass(item.status === 'answered' ? 'info' : 'warning')}">${escapeHtml(item.status || 'resolved')}</span>
          </div>
          <div class="hp2-muted">${escapeHtml(resolution)}</div>
          ${attachmentCount ? `<div class="hp2-muted">Media: ${attachmentCount}</div>` : ''}
          ${renderDrawerAttachmentLinks(item.attachments)}
          <div class="hp2-queue-actions">
            ${item.status === 'answered' && !item.published_question_id ? `<button type="button" class="hp2-btn hp2-btn-ghost" data-q-action="limbo-publish" data-id="${id}">Publish</button>` : ''}
          </div>
        </li>`;
      }).join('') : queueListEmpty('No resolved limbo items.');

      if (evidenceEl) {
        evidenceEl.innerHTML = visibleEvidence.length ? visibleEvidence.map((item) => {
          const title = String(item?.title || '').trim() || 'Evidence item';
          const category = String(item?.category || 'system');
          const severity = String(item?.severity || 'medium');
          const summary = String(item?.summary || '').trim();
          const attachmentCount = Array.isArray(item?.attachments) ? item.attachments.length : 0;
          return `<li>
            <div class="hp2-feed-item-row">
              <strong>${escapeHtml(title)}</strong>
              <span class="${severityClass(severity === 'critical' || severity === 'high' ? 'warning' : 'info')}">${escapeHtml(`${category}/${severity}`)}</span>
            </div>
            ${summary ? `<div class="hp2-muted">${escapeHtml(summary)}</div>` : ''}
            <div class="hp2-muted">${escapeHtml(fmtDate(item?.created_at))}</div>
            ${attachmentCount ? `<div class="hp2-muted">Media: ${attachmentCount}</div>` : ''}
            ${renderDrawerAttachmentLinks(item?.attachments)}
          </li>`;
        }).join('') : queueListEmpty('No evidence logs match the current media filter.');
      }

      if (correctionsEl) {
        correctionsEl.innerHTML = corrections.length ? corrections.slice(0, 120).map((item) => {
          const eventType = String(item?.event_type || 'update').replaceAll('_', ' ');
          const note = String(item?.note || '').trim();
          const actor = String(item?.actor || 'system').trim();
          const targetType = String(item?.target_type || 'item').trim();
          const targetId = String(item?.target_id || '').trim();
          return `<li>
            <div class="hp2-feed-item-row">
              <strong>${escapeHtml(eventType)}</strong>
              <span class="${severityClass('info')}">${escapeHtml(`${targetType}${targetId ? `#${targetId}` : ''}`)}</span>
            </div>
            ${note ? `<div class="hp2-muted">${escapeHtml(note)}</div>` : ''}
            <div class="hp2-muted">${escapeHtml(actor)} • ${escapeHtml(fmtDate(item?.created_at))}</div>
          </li>`;
        }).join('') : queueListEmpty('No correction events logged yet.');
      }

      state.telemetry.drawerAt = Date.now();
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        pendingEl.innerHTML = queueListEmpty('Failed to load pending limbo.');
        resolvedEl.innerHTML = queueListEmpty('Failed to load resolved limbo.');
        if (evidenceEl) evidenceEl.innerHTML = queueListEmpty('Failed to load evidence logs.');
        if (correctionsEl) correctionsEl.innerHTML = queueListEmpty('Failed to load correction events.');
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
      setDrawerResult('Limbo answer cannot be empty.');
      return;
    }
    const normalized = response.value;
    setDrawerResult('Saving limbo answer...');
    try {
      await apiPost(`/api/handler/limbo/${encodeURIComponent(String(itemId))}/answer`, { answer_text: normalized });
      setDrawerResult('Limbo item answered.');
      await loadEvidenceDrawer();
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setDrawerResult(`Failed to answer limbo item: ${err.message}`);
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
    setDrawerResult('Dismissing limbo item...');
    try {
      await apiPost(`/api/handler/limbo/${encodeURIComponent(String(itemId))}/dismiss`, { reason });
      setDrawerResult('Limbo item dismissed.');
      await loadEvidenceDrawer();
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setDrawerResult(`Failed to dismiss limbo item: ${err.message}`);
      }
    }
  }

  async function publishQueueLimbo(itemId) {
    setDrawerResult('Publishing limbo item...');
    try {
      const result = await apiPost(`/api/handler/limbo/${encodeURIComponent(String(itemId))}/publish`, {});
      if (result && result.already_published) {
        setDrawerResult('Limbo item was already published.');
      } else {
        setDrawerResult('Limbo item published.');
      }
      await loadEvidenceDrawer();
      await loadQueueQuestions();
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setDrawerResult(`Failed to publish limbo item: ${err.message}`);
      }
    }
  }

  async function loadQueueHub() {
    await Promise.allSettled([
      loadQueueBooking(),
      loadQueueMailThreads(),
      loadQueueQuestions(),
      loadQueueEscalations(),
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
      return;
    }
    if (action === 'escalation-resolve') {
      await resolveQueueEscalation(id);
      return;
    }
    if (action === 'escalation-activate') {
      await activateQueueEscalation(id);
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
      danger: action === 'PAVLOK_COMMAND' && ['shock', 'zap'].includes(String(payload?.pavlok_cmd || '').toLowerCase()),
    });
    if (!confirmed.confirmed) return;

    setInlineResult('hp2-action-result', `${title} sending...`);
    const commandId = makeCommandId('quick');
    try {
      const normalizedPayload = normalizePushFields(payload);
      await apiPost('/api/handler/tpe/push', {
        device_id: state.selectedDeviceId,
        command_id: commandId,
        action,
        ...normalizedPayload,
      });
      setInlineResult('hp2-action-result', successMessage);
      recordCommandHistory(title, historyDetail || `${title} for ${label}`, true, {
        commandId,
        statusLabel: 'sent',
      });
      pushFeed(`${title} sent to ${state.selectedDeviceId}`);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult('hp2-action-result', `Failed: ${err.message}`);
        recordCommandHistory(title, `${title} failed for ${label}: ${err.message}`, false);
      }
    }
  }

  function normalizePushFields(fields) {
    if (!fields || typeof fields !== 'object') return {};
    const normalized = {};
    Object.entries(fields).forEach(([rawKey, rawValue]) => {
      const key = String(rawKey || '').trim();
      if (!key || rawValue == null) return;
      if (typeof rawValue === 'string') {
        normalized[key] = rawValue;
      } else if (typeof rawValue === 'number' || typeof rawValue === 'boolean') {
        normalized[key] = String(rawValue);
      } else {
        try {
          normalized[key] = JSON.stringify(rawValue);
        } catch (_err) {
          normalized[key] = String(rawValue);
        }
      }
    });

    // Legacy convenience aliases for clients that still provide generic keys.
    if (!normalized.toy_command && normalized.command) {
      normalized.toy_command = normalized.command;
    }
    if (!normalized.toy_level && normalized.intensity) {
      normalized.toy_level = normalized.intensity;
    }
    if (!normalized.toy_duration_ms && normalized.duration_ms) {
      normalized.toy_duration_ms = normalized.duration_ms;
    }
    return normalized;
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
    const commandId = makeCommandId('cmd');
    try {
      await apiPost('/api/handler/tpe/push', {
        device_id: state.selectedDeviceId,
        command_id: commandId,
        action,
        ...fields,
      });
      setInlineResult(resultId, `${title} sent.`);
      recordCommandHistory(title, historyDetail || `${action} for ${selectedDeviceLabel()}`, true, {
        commandId,
        statusLabel: 'sent',
      });
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult(resultId, `Failed: ${err.message}`);
        recordCommandHistory(title, `${title} failed: ${err.message}`, false);
      }
    }
  }

  async function sendAppLifecycleCommand() {
    const action = String(byId('hp2-appctl-action')?.value || '').trim();
    if (!action) {
      setInlineResult('hp2-appctl-result', 'Choose an app action first.');
      return;
    }

    let dynamicFields = {};
    try {
      dynamicFields = readAppActionFieldValues(action);
    } catch (err) {
      setInlineResult('hp2-appctl-result', err?.message || 'Required app fields are missing.');
      return;
    }

    const appNameRequired = action !== 'APP_LIST_POLL';
    const legacyAppName = String(byId('hp2-appctl-name')?.value || '').trim();
    if (appNameRequired && !dynamicFields.app_name && legacyAppName) {
      dynamicFields.app_name = legacyAppName;
    }
    if (appNameRequired && !dynamicFields.app_name) {
      setInlineResult('hp2-appctl-result', 'App name is required.');
      return;
    }

    const appName = String(dynamicFields.app_name || '').trim();
    const title = action.replaceAll('_', ' ');
    const dangerActions = new Set(['FORCE_STOP_APP', 'DISABLE_APP', 'UNINSTALL_APP', 'SUSPEND_APP']);
    await sendControlCommand({
      title,
      action,
      fields: dynamicFields,
      resultId: 'hp2-appctl-result',
      confirmText: 'Send',
      danger: dangerActions.has(action),
      message: appName ? `${title} for ${appName} on ${selectedDeviceLabel()}?` : `${title} on ${selectedDeviceLabel()}?`,
      historyDetail: appName ? `${title} app=${appName}` : `${title}`,
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

  function summarizeFieldsForHistory(fields) {
    const keys = Object.keys(fields || {});
    if (!keys.length) return '';
    return keys
      .slice(0, 3)
      .map((key) => `${key}=${String(fields[key]).slice(0, 40)}`)
      .join(' ');
  }

  async function sendSchemaScreenAction() {
    const action = String(byId('hp2-screenctl-action')?.value || '').trim();
    if (!action) {
      setInlineResult('hp2-screenctl-result', 'Choose a screen action first.');
      return;
    }

    let fields = {};
    try {
      fields = readScreenActionFieldValues(action);
    } catch (err) {
      setInlineResult('hp2-screenctl-result', err?.message || 'Required screen fields are missing.');
      return;
    }

    if (action === 'SET_BRIGHTNESS' && !fields.value) {
      fields.value = String(Math.max(0, Math.min(255, Number(byId('hp2-screenctl-brightness')?.value || 150))));
    }
    if (action === 'SET_SCREEN_TIMEOUT' && !fields.ms) {
      fields.ms = String(Math.max(1000, Math.min(86400000, Number(byId('hp2-screenctl-timeout')?.value || 120000))));
    }
    if (action === 'SET_AUTO_ROTATE' && !fields.enabled) {
      fields.enabled = String(byId('hp2-screenctl-autorotate')?.value || 'true') === 'true' ? 'true' : 'false';
    }
    if (action === 'OPEN_URL' && !fields.url) {
      fields.url = String(byId('hp2-screenctl-url')?.value || '').trim();
    }
    if (action === 'SHOW_OVERLAY') {
      if (!fields.title) fields.title = String(byId('hp2-screenctl-overlay-title')?.value || '').trim();
      if (!fields.message) fields.message = String(byId('hp2-screenctl-overlay-message')?.value || '').trim();
      if (!fields.image_url) fields.image_url = String(byId('hp2-screenctl-overlay-image')?.value || '').trim();
      if (!fields.title) fields.title = 'Check-in Requested';
      if (!fields.message) fields.message = 'Please open the app and respond.';
      if (!fields.image_url) delete fields.image_url;
    }

    const title = action.replaceAll('_', ' ');
    const fieldSummary = summarizeFieldsForHistory(fields);
    await sendScreenLockAction(action, {
      title,
      fields,
      danger: action === 'LOCK_DEVICE',
      confirmText: action === 'LOCK_DEVICE' ? 'Lock' : 'Send',
      message: `${title} for ${selectedDeviceLabel()}?`,
      historyDetail: fieldSummary ? `${action} ${fieldSummary}` : action,
    });
  }

  async function sendQuickTapCommand() {
    if (!state.selectedDeviceId) {
      setInlineResult('hp2-quicktap-result', 'Select a device first.');
      return;
    }
    const target = state.commands.liveControl.quickTapTarget;
    const allowedActions = quickTapActionsForTarget(target);
    let action = state.commands.liveControl.quickTapAction;
    if (!allowedActions.includes(action)) {
      [action] = allowedActions;
      state.commands.liveControl.quickTapAction = action;
      renderQuickTapActionButtons();
    }
    const pavlokCmd = normalizePavlokCommand(action);
    const lovenseCmd = normalizeLovenseCommand(action);
    const intensity = Number(byId('hp2-quicktap-intensity')?.value || 10);
    const length = Number(byId('hp2-quicktap-length')?.value || 800);
    const loop = Math.max(1, Math.min(12, Number(byId('hp2-quicktap-loop')?.value || 1)));

    const confirm = await askInlineConfirm({
      title: 'Send Quick Tap',
      message: `${target} ${action} intensity ${intensity} loop ${loop} for ${selectedDeviceLabel()}?`,
      confirmText: 'Send',
      danger: target === 'pavlok' && pavlokCmd === 'zap',
    });
    if (!confirm.confirmed) return;

    setInlineResult('hp2-quicktap-result', 'Sending quick tap...');
    try {
      const baseCommandId = makeCommandId('quicktap');
      const interTapDelayMs = target === 'pavlok'
        ? (pavlokCmd === 'zap' ? 700 : 450)
        : Math.max(220, Math.min(1200, Math.floor(length / 2)));

      for (let i = 0; i < loop; i += 1) {
        const loopCommandId = `${baseCommandId}-${i + 1}`;
        if (target === 'pavlok') {
          await apiPost('/api/handler/tpe/push', {
            device_id: state.selectedDeviceId,
            command_id: loopCommandId,
            action: 'PAVLOK_COMMAND',
            payload: {
              pavlok_cmd: pavlokCmd,
              pavlok_intensity: String(intensity),
              intensity: String(intensity),
              toy_level: String(intensity),
              pavlok_duration_ms: String(Math.max(200, length)),
              duration_ms: String(Math.max(200, length)),
              toy_duration_ms: String(Math.max(200, length)),
            },
            pavlok_cmd: pavlokCmd,
            pavlok_intensity: String(intensity),
            intensity: String(intensity),
            toy_level: String(intensity),
            pavlok_duration_ms: String(Math.max(200, length)),
            duration_ms: String(Math.max(200, length)),
            toy_duration_ms: String(Math.max(200, length)),
          });
        } else {
          await apiPost('/api/handler/tpe/push', {
            device_id: state.selectedDeviceId,
            command_id: loopCommandId,
            action: 'LOVENSE_COMMAND',
            payload: {
              command: lovenseCmd,
              toy_command: lovenseCmd,
              intensity,
              toy_level: intensity,
              level: intensity,
              length,
              duration_ms: length,
              toy_duration_ms: length,
            },
            command: lovenseCmd,
            toy_command: lovenseCmd,
            intensity,
            toy_level: intensity,
            level: intensity,
            length,
            duration_ms: length,
            toy_duration_ms: length,
          });
        }

        if (loop > 1 && i < loop - 1) {
          setInlineResult('hp2-quicktap-result', `Sending quick tap... (${i + 1}/${loop})`);
          await waitMs(interTapDelayMs);
        }
      }
      setInlineResult('hp2-quicktap-result', `Quick tap sent (${loop}x).`);
      recordCommandHistory('Quick Tap', `${target} ${action} intensity ${intensity}${pavlokCmd !== 'zap' ? ` length ${length}ms` : ''} loop ${loop}`, true, {
        commandId: `${baseCommandId}-1`,
        statusLabel: 'sent',
      });
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
      const commandId = makeCommandId('lovense-live');
      await apiPost('/api/handler/tpe/push', {
        device_id: state.selectedDeviceId,
        command_id: commandId,
        action: 'toy.live.control',
        toy_mode: 'lovense',
        toy_command: 'vibrate',
        toy_level: String(level),
        toy_duration_ms: String(duration),
        ...(pattern ? { toy_pattern: pattern } : {}),
      });
      setInlineResult('hp2-lovense-result', 'Lovense live pattern sent.');
      recordCommandHistory('Lovense Live', `${pattern || 'steady'} level ${level} duration ${duration}ms`, true, {
        commandId,
        statusLabel: 'sent',
      });
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
      const commandId = makeCommandId('lovense-ramp');
      await apiPost('/api/handler/tpe/push', {
        device_id: state.selectedDeviceId,
        command_id: commandId,
        action: 'toy.live.control',
        toy_mode: 'lovense',
        toy_command: 'vibrate',
        toy_level: String(minLevel),
        toy_sequence: JSON.stringify(full),
      });
      setInlineResult('hp2-lovense-result', 'Live up/down ramp sent.');
      recordCommandHistory('Lovense Ramp', `min ${minLevel} max ${maxLevel} step ${stepMs}ms loops ${loops}`, true, {
        commandId,
        statusLabel: 'sent',
      });
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
      const commandId = makeCommandId('lovense-schedule');
      await apiPost('/api/handler/tpe/push', {
        device_id: state.selectedDeviceId,
        command_id: commandId,
        action: 'SET_LOVENSE_SCHEDULES',
        schedules: JSON.stringify(parsed),
      });
      setInlineResult('hp2-lovense-result', 'Lovense schedule sent.');
      recordCommandHistory('Lovense Timed', `${parsed.length} schedule row(s)`, true, {
        commandId,
        statusLabel: 'sent',
      });
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
    const cmdRaw = String(byId('hp2-pavlok-command')?.value || 'shock').toLowerCase();
    const cmd = normalizePavlokCommand(cmdRaw);
    const intensity = Math.max(0, Math.min(255, Number(byId('hp2-pavlok-intensity')?.value || 60)));
    const duration = Math.max(100, Number(byId('hp2-pavlok-duration')?.value || 1000));

    setInlineResult('hp2-pavlok-result', 'Sending Pavlok command...');
    try {
      const commandId = makeCommandId('pavlok');
      await apiPost('/api/handler/tpe/push', {
        device_id: state.selectedDeviceId,
        command_id: commandId,
        action: 'PAVLOK_COMMAND',
        payload: {
          pavlok_cmd: cmd,
          pavlok_intensity: String(intensity),
          intensity: String(intensity),
          toy_level: String(intensity),
          ...(cmd !== 'zap' && cmd !== 'stop' ? {
            pavlok_duration_ms: String(duration),
            duration_ms: String(duration),
            toy_duration_ms: String(duration),
          } : {}),
        },
        pavlok_cmd: cmd,
        pavlok_intensity: String(intensity),
        intensity: String(intensity),
        toy_level: String(intensity),
        ...(cmd !== 'zap' && cmd !== 'stop' ? {
          pavlok_duration_ms: String(duration),
          duration_ms: String(duration),
          toy_duration_ms: String(duration),
        } : {}),
      });
      setInlineResult('hp2-pavlok-result', 'Pavlok command sent.');
      recordCommandHistory('Pavlok Precision', `${cmdRaw} intensity ${intensity}${cmd !== 'zap' && cmd !== 'stop' ? ` duration ${duration}ms` : ''}`, true, {
        commandId,
        statusLabel: 'sent',
      });
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

  async function sendShowOverlay() {
    const title = String(byId('hp2-screenctl-overlay-title')?.value || '').trim() || 'Check-in Requested';
    const message = String(byId('hp2-screenctl-overlay-message')?.value || '').trim() || 'Please open the app and respond.';
    const imageUrl = String(byId('hp2-screenctl-overlay-image')?.value || '').trim();
    await sendScreenLockAction('SHOW_OVERLAY', {
      title: 'Show Overlay',
      fields: {
        title,
        message,
        ...(imageUrl ? { image_url: imageUrl } : {}),
      },
      message: `Show overlay on ${selectedDeviceLabel()}?`,
      historyDetail: `SHOW_OVERLAY title=${title}`,
    });
  }

  async function sendSchemaNotifyAction() {
    const action = String(byId('hp2-notify-action')?.value || '').trim();
    if (!action) {
      setInlineResult('hp2-notify-result', 'Choose a notify action first.');
      return;
    }

    let fields = {};
    try {
      fields = readNotifyActionFieldValues(action);
    } catch (err) {
      setInlineResult('hp2-notify-result', err?.message || 'Required notify fields are missing.');
      return;
    }

    if (action === 'SEND_NOTIFICATION') {
      if (!fields.title) fields.title = String(byId('hp2-notify-title')?.value || '').trim();
      if (!fields.body) fields.body = String(byId('hp2-notify-body')?.value || '').trim();
      if (!fields.channel_id) fields.channel_id = String(byId('hp2-notify-channel')?.value || '').trim();
      if (!fields.title) {
        setInlineResult('hp2-notify-result', 'Notification title is required.');
        return;
      }
      if (!fields.body) delete fields.body;
      if (!fields.channel_id) delete fields.channel_id;
    }

    if (action === 'SPEAK_TEXT') {
      if (!fields.text) fields.text = String(byId('hp2-speak-text')?.value || '').trim();
      if (!fields.text) {
        setInlineResult('hp2-notify-result', 'Speak text is required.');
        return;
      }
    }

    const title = action.replaceAll('_', ' ');
    const fieldSummary = summarizeFieldsForHistory(fields);
    await sendControlCommand({
      title,
      action,
      fields,
      resultId: 'hp2-notify-result',
      message: `${title} for ${selectedDeviceLabel()}?`,
      historyDetail: fieldSummary ? `${action} ${fieldSummary}` : action,
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
        toy_command: 'vibrate',
        toy_level: '8',
        toy_duration_ms: '700',
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
        toy_command: 'vibrate',
        toy_level: '20',
        toy_duration_ms: '500',
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
        pavlok_cmd: 'zap',
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

  function isAdminRole() {
    return String(state.role || '').toLowerCase() === 'admin';
  }

  function applyAdminBulkDeviceControlsAccess() {
    const isAdmin = isAdminRole();
    const reason = isAdmin ? '' : 'Admin role required.';
    setButtonEnabled('hp2-devices-cleanup-stale-btn', isAdmin, reason);
    setButtonEnabled('hp2-devices-maintenance-run-btn', isAdmin, reason);
    setButtonEnabled('hp2-devices-delete-all-btn', isAdmin, reason);
    if (!isAdmin) {
      setInlineResult('hp2-devices-bulk-result', 'Only admins can run bulk device operations.');
      setInlineResult('hp2-devices-maintenance-status', 'Weekly hygiene status requires admin access.');
      return;
    }
    loadDeviceMaintenanceStatus().catch(() => {});
  }

  async function loadDeviceMaintenanceStatus() {
    if (!isAdminRole()) return;
    try {
      const status = await apiGet('/api/handler/devices/maintenance/status');
      const last = status?.last_run;
      const nextRun = status?.next_run_utc ? fmtDate(status.next_run_utc) : 'n/a';
      if (!last) {
        setInlineResult('hp2-devices-maintenance-status', `Weekly hygiene ready. Next scheduled run: ${nextRun}.`);
        return;
      }
      const deleted = Number(last.deleted || 0);
      const candidates = Number(last.candidates || 0);
      const when = fmtDate(last.created_at);
      setInlineResult(
        'hp2-devices-maintenance-status',
        `Last hygiene ${last.status || 'ok'} at ${when}: deleted ${deleted}/${candidates}. Next: ${nextRun}.`,
      );
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult('hp2-devices-maintenance-status', `Maintenance status unavailable: ${err.message}`);
      }
    }
  }

  async function runDeviceMaintenanceNow() {
    if (!isAdminRole()) {
      setInlineResult('hp2-devices-bulk-result', 'Only admins can run maintenance cleanup.');
      return;
    }
    setInlineResult('hp2-devices-bulk-result', 'Running weekly hygiene cleanup...');
    try {
      const result = await apiPost('/api/handler/devices/maintenance/run-now?older_than_hours=168&include_name_fallback=true', {});
      const deleted = Number(result?.deleted || 0);
      const candidates = Number(result?.candidates || 0);
      const remaining = Number(result?.remaining || 0);
      setInlineResult('hp2-devices-bulk-result', `Hygiene run complete: deleted ${deleted}/${candidates}. Remaining devices: ${remaining}.`);
      await loadDevices();
      await loadDeviceMaintenanceStatus();
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult('hp2-devices-bulk-result', `Maintenance run failed: ${err.message}`);
      }
    }
  }

  async function cleanupStaleDevicesFromPanel() {
    if (!isAdminRole()) {
      setInlineResult('hp2-devices-bulk-result', 'Only admins can run stale cleanup.');
      return;
    }

    const hoursInput = await askInlineText({
      title: 'Cleanup Stale Duplicates',
      message: 'Enter staleness threshold hours (1-8760). Dry-run happens first.',
      inputLabel: 'Older Than Hours',
      inputValue: '72',
      confirmText: 'Dry Run',
    });
    if (!hoursInput.confirmed) return;
    if (hoursInput.invalidEmpty) {
      setInlineResult('hp2-devices-bulk-result', 'Hours value is required.');
      return;
    }

    const olderThanHours = Number.parseInt(String(hoursInput.value || '').trim(), 10);
    if (!Number.isFinite(olderThanHours) || olderThanHours < 1 || olderThanHours > 8760) {
      setInlineResult('hp2-devices-bulk-result', 'Hours must be between 1 and 8760.');
      return;
    }

    setInlineResult('hp2-devices-bulk-result', 'Running stale cleanup dry-run...');

    let dryRun;
    try {
      const dryResp = await apiFetch(
        `/api/handler/devices/cleanup-stale?older_than_hours=${encodeURIComponent(String(olderThanHours))}&include_name_fallback=true&dry_run=true`,
        { method: 'POST' },
      );
      dryRun = await dryResp.json().catch(() => ({}));
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult('hp2-devices-bulk-result', `Dry-run failed: ${err.message}`);
      }
      return;
    }

    const candidates = Number(dryRun?.candidates || 0);
    if (candidates <= 0) {
      setInlineResult('hp2-devices-bulk-result', 'Dry-run found no stale duplicate device rows.');
      return;
    }

    const confirm = await askInlineConfirm({
      title: 'Apply Stale Cleanup',
      message: `Dry-run found ${candidates} stale duplicate row(s). Delete now?`,
      confirmText: 'Delete Stale',
      danger: true,
    });
    if (!confirm.confirmed) {
      setInlineResult('hp2-devices-bulk-result', `Cancelled. ${candidates} candidate row(s) unchanged.`);
      return;
    }

    setInlineResult('hp2-devices-bulk-result', 'Deleting stale duplicate rows...');
    try {
      const runResp = await apiFetch(
        `/api/handler/devices/cleanup-stale?older_than_hours=${encodeURIComponent(String(olderThanHours))}&include_name_fallback=true&dry_run=false`,
        { method: 'POST' },
      );
      const result = await runResp.json().catch(() => ({}));
      const deleted = Number(result?.deleted || 0);
      const remaining = Number(result?.remaining || 0);

      await loadDevices();
      setInlineResult('hp2-devices-bulk-result', `Deleted ${deleted} stale duplicate row(s). Remaining devices: ${remaining}.`);
      pushFeed(`Stale cleanup removed ${deleted} device row(s).`);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult('hp2-devices-bulk-result', `Cleanup failed: ${err.message}`);
      }
    }
  }

  async function deleteAllDevicesFromPanel() {
    if (!isAdminRole()) {
      setInlineResult('hp2-devices-bulk-result', 'Only admins can delete all devices.');
      return;
    }

    const expectedPhrase = 'DELETE ALL DEVICES';
    setInlineResult('hp2-devices-bulk-result', 'Loading delete-all impact preview...');
    let preview;
    try {
      preview = await apiPost('/api/handler/devices/delete-all/preview', {});
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult('hp2-devices-bulk-result', `Preview failed: ${err.message}`);
      }
      return;
    }

    const statusRows = Number(preview?.status_rows || 0);
    const assignmentRows = Number(preview?.assignment_rows || 0);
    const pairingRows = Number(preview?.pairing_rows || 0);
    const sample = Array.isArray(preview?.sample_device_ids) ? preview.sample_device_ids.slice(0, 5).join(', ') : '';

    const preConfirm = await askInlineConfirm({
      title: 'Delete All Devices (Preview)',
      message: `Will delete status=${statusRows}, assignments=${assignmentRows}, pairings=${pairingRows}.${sample ? ` Sample: ${sample}` : ''}`,
      confirmText: 'Continue',
      danger: true,
    });
    if (!preConfirm.confirmed) {
      setInlineResult('hp2-devices-bulk-result', 'Delete-all cancelled after preview.');
      return;
    }

    const phraseInput = await askInlineText({
      title: 'Delete All Devices',
      message: `This permanently removes all tracked devices. Type ${expectedPhrase} to continue.`,
      inputLabel: 'Confirmation Phrase',
      inputPlaceholder: expectedPhrase,
      confirmText: 'Delete All',
      danger: true,
    });
    if (!phraseInput.confirmed) return;
    if (phraseInput.invalidEmpty) {
      setInlineResult('hp2-devices-bulk-result', 'Confirmation phrase is required.');
      return;
    }

    const providedPhrase = String(phraseInput.value || '').trim();
    if (providedPhrase !== expectedPhrase) {
      setInlineResult('hp2-devices-bulk-result', `Confirmation mismatch. Type ${expectedPhrase} exactly.`);
      return;
    }

    setInlineResult('hp2-devices-bulk-result', 'Deleting all tracked devices...');
    try {
      const result = await apiPost('/api/handler/devices/delete-all', {
        confirm_phrase: providedPhrase,
      });

      const deletedDevices = Number(result?.deleted_device_count || 0);
      const deletedAssignments = Number(result?.deleted_assignment_rows || 0);
      const deletedPairings = Number(result?.deleted_pairing_rows || 0);

      await loadDevices();
      setInlineResult(
        'hp2-devices-bulk-result',
        `Deleted ${deletedDevices} devices, ${deletedAssignments} assignments, ${deletedPairings} pairings.`,
      );
      pushFeed(`Admin wipe removed ${deletedDevices} devices.`);
    } catch (err) {
      if (err.message !== AUTH_EXPIRED_ERROR) {
        setInlineResult('hp2-devices-bulk-result', `Delete-all failed: ${err.message}`);
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
    loadDeviceApps().catch(() => {});
    loadDashboardIntelligence().catch(() => {});
    pushFeed(`Selected ${state.selectedDeviceId}`);
  }

  function bindEvents() {
    const bind = (id, eventName, handler) => {
      const el = byId(id);
      if (!el) return;
      el.addEventListener(eventName, handler);
    };

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
    byId('hp2-appctl-action').addEventListener('change', renderAppActionFieldInputs);
    byId('hp2-screenctl-send-schema-btn').addEventListener('click', () => sendSchemaScreenAction().catch(() => {}));
    byId('hp2-screenctl-action').addEventListener('change', renderScreenActionFieldInputs);
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
    byId('hp2-screenctl-overlay-send-btn').addEventListener('click', () => sendShowOverlay().catch(() => {}));
    byId('hp2-notify-send-schema-btn').addEventListener('click', () => sendSchemaNotifyAction().catch(() => {}));
    byId('hp2-notify-action').addEventListener('change', renderNotifyActionFieldInputs);
    byId('hp2-notify-send-btn').addEventListener('click', () => sendNotificationCommand().catch(() => {}));
    byId('hp2-notify-clear-btn').addEventListener('click', () => clearNotificationsCommand().catch(() => {}));
    byId('hp2-speak-send-btn').addEventListener('click', () => speakTextCommand().catch(() => {}));
    byId('hp2-clipboard-send-btn').addEventListener('click', () => sendClipboardCommand().catch(() => {}));
    byId('hp2-sms-inject-btn').addEventListener('click', () => injectProxySmsCommand().catch(() => {}));
    byId('hp2-sms-reply-toggle-btn').addEventListener('click', () => setSmsReplyPermissionCommand().catch(() => {}));
    byId('hp2-sms-thread-refresh-btn').addEventListener('click', () => refreshSmsThreadPresets().catch(() => {}));
    byId('hp2-macro-save-btn').addEventListener('click', () => saveMacroFromInputs().catch(() => {}));
    byId('hp2-macro-run-btn').addEventListener('click', () => runSelectedMacro().catch(() => {}));
    byId('hp2-macro-delete-btn').addEventListener('click', () => deleteSelectedMacro().catch(() => {}));
    byId('hp2-macro-select').addEventListener('change', (event) => {
      selectMacroById(event.target.value || '');
    });
    byId('hp2-startle-btn').addEventListener('click', () => startleSelected().catch(() => {}));
    byId('hp2-shock-10-btn').addEventListener('click', () => shockSelected(10).catch(() => {}));
    byId('hp2-shock-30-btn').addEventListener('click', () => shockSelected(30).catch(() => {}));
    byId('hp2-shock-60-btn').addEventListener('click', () => shockSelected(60).catch(() => {}));
    byId('hp2-mobile-lock-btn')?.addEventListener('click', () => lockSelected().catch(() => {}));
    byId('hp2-mobile-checkin-btn')?.addEventListener('click', () => requestCheckin().catch(() => {}));
    byId('hp2-mobile-startle-btn')?.addEventListener('click', () => startleSelected().catch(() => {}));
    byId('hp2-rename-device-btn').addEventListener('click', () => renameSelectedDevice().catch(() => {}));
    byId('hp2-devices-cleanup-stale-btn')?.addEventListener('click', () => cleanupStaleDevicesFromPanel().catch(() => {}));
    byId('hp2-devices-maintenance-run-btn')?.addEventListener('click', () => runDeviceMaintenanceNow().catch(() => {}));
    byId('hp2-devices-delete-all-btn')?.addEventListener('click', () => deleteAllDevicesFromPanel().catch(() => {}));
    byId('hp2-device-apps-poll-btn')?.addEventListener('click', () => pollDeviceApps().catch(() => {}));
    byId('hp2-device-apps-refresh-btn')?.addEventListener('click', () => loadDeviceApps().catch(() => {}));
    byId('hp2-device-apps-search')?.addEventListener('input', () => {
      loadDeviceApps().catch(() => {});
    });
    byId('hp2-device-apps-system-filter')?.addEventListener('change', () => {
      loadDeviceApps().catch(() => {});
    });
    byId('hp2-vpn-poll-btn')?.addEventListener('click', () => {
      sendVpnStatusAction('VPN_STATUS_POLL').catch(() => {});
    });
    byId('hp2-vpn-connect-btn')?.addEventListener('click', () => {
      sendVpnStatusAction('VPN_CONNECT').catch(() => {});
    });
    byId('hp2-vpn-disconnect-btn')?.addEventListener('click', () => {
      sendVpnStatusAction('VPN_DISCONNECT').catch(() => {});
    });
    byId('hp2-refresh-btn').addEventListener('click', () => hydrateApp().catch(() => {}));
    byId('hp2-autofollow-btn').addEventListener('click', toggleAutoFollow);
    byId('hp2-refresh-intel-btn').addEventListener('click', () => loadDashboardIntelligence().catch(() => {}));
    byId('hp2-hard-refresh-btn').addEventListener('click', () => hydrateApp().catch(() => {}));
    byId('hp2-drawer-limbo-add-btn').addEventListener('click', () => createDrawerLimboItem().catch(() => {}));
    byId('hp2-drawer-evidence-add-btn').addEventListener('click', () => createDrawerEvidenceItem().catch(() => {}));
    bind('hp2-ai-save-btn', 'click', () => saveAiWardenConfig().catch(() => {}));
    bind('hp2-ai-refresh-btn', 'click', () => loadAiWardenDashboard().catch(() => {}));
    bind('hp2-public-use-save-btn', 'click', () => savePublicUseSettings().catch(() => {}));
    bind('hp2-public-use-panic-15-btn', 'click', () => setPublicUsePanic(15).catch(() => {}));
    bind('hp2-public-use-panic-clear-btn', 'click', () => setPublicUsePanic(0).catch(() => {}));
    bind('hp2-public-use-refresh-analytics-btn', 'click', () => loadPublicUseAnalytics().catch(() => {}));

    document.querySelectorAll('[data-cmd-preset]').forEach((button) => {
      button.addEventListener('click', () => {
        applyCommandPreset(button.dataset.cmdPreset || '');
      });
    });

    document.querySelectorAll('[data-cmd-section-target]').forEach((button) => {
      button.addEventListener('click', () => {
        setCommandSection(button.dataset.cmdSectionTarget || 'all');
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
        const dynamicName = byId('hp2-appctl-param-app_name');
        if (dynamicName) {
          dynamicName.value = button.dataset.appPreset || '';
        }
      });
    });

    ['hp2-quicktap-intensity', 'hp2-lovense-live-level', 'hp2-lovense-ramp-min', 'hp2-lovense-ramp-max', 'hp2-pavlok-intensity', 'hp2-pavlok-command', 'hp2-screenctl-brightness']
      .forEach((id) => {
        const el = byId(id);
        if (!el) return;
        el.addEventListener('input', syncControlReadouts);
        el.addEventListener('change', syncControlReadouts);
      });

    byId('hp2-settings-save-btn').addEventListener('click', async () => {
      const saveBtn = byId('hp2-settings-save-btn');
      if (saveBtn) saveBtn.disabled = true;
      setInlineResult('hp2-settings-result', 'Saving...');
      try {
        const result = await saveSettingsFromForm();
        if (result.publicStatusSaved) {
          setInlineResult('hp2-settings-result', 'Settings saved. Days Locked start date updated.');
        } else {
          setInlineResult('hp2-settings-result', 'Settings saved locally. Days Locked start date update failed.');
        }
      } catch (err) {
        if (err.message !== AUTH_EXPIRED_ERROR) {
          setInlineResult('hp2-settings-result', `Save failed: ${err.message}`);
        }
      } finally {
        if (saveBtn) saveBtn.disabled = false;
      }
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
    bind('hp2-drawer-media-filter', 'click', (event) => {
      const btn = event.target.closest('[data-drawer-media-filter]');
      if (!btn) return;
      setDrawerMediaFilter(btn.dataset.drawerMediaFilter || 'all');
    });

    byId('hp2-queue-booking-filter').addEventListener('change', () => loadQueueBooking().catch(() => {}));
    byId('hp2-queue-mail-filter').addEventListener('change', () => loadQueueMailThreads().catch(() => {}));
    byId('hp2-queue-escalation-filter').addEventListener('change', () => loadQueueEscalations().catch(() => {}));
    byId('hp2-queue-escalation-refresh').addEventListener('click', () => loadQueueEscalations().catch(() => {}));
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
    byId('hp2-drawer-lightbox-close').addEventListener('click', () => {
      closeDrawerLightbox();
    });
    byId('hp2-drawer-lightbox-prev').addEventListener('click', () => {
      navigateDrawerLightbox(-1);
    });
    byId('hp2-drawer-lightbox-next').addEventListener('click', () => {
      navigateDrawerLightbox(1);
    });
    byId('hp2-drawer-lightbox').addEventListener('click', (event) => {
      const target = event.target;
      if (target && target.dataset && target.dataset.drawerLightboxDismiss === 'true') {
        closeDrawerLightbox();
      }
    });

    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && isDrawerLightboxOpen()) {
        event.preventDefault();
        closeDrawerLightbox();
        return;
      }
      if (event.key === 'ArrowLeft' && isDrawerLightboxOpen()) {
        event.preventDefault();
        navigateDrawerLightbox(-1);
        return;
      }
      if (event.key === 'ArrowRight' && isDrawerLightboxOpen()) {
        event.preventDefault();
        navigateDrawerLightbox(1);
        return;
      }
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

    ['hp2-queue-booking-list', 'hp2-queue-mail-list', 'hp2-queue-mail-messages', 'hp2-queue-questions-open', 'hp2-queue-questions-answered', 'hp2-queue-escalation-list', 'hp2-drawer-limbo-pending', 'hp2-drawer-limbo-resolved', 'hp2-drawer-evidence-list']
      .forEach((id) => {
        const host = byId(id);
        if (!host) return;
        host.addEventListener('click', (event) => {
          const lightboxTrigger = event.target.closest('[data-drawer-lightbox-src]');
          if (lightboxTrigger) {
            event.preventDefault();
            const gallery = collectDrawerLightboxGallery();
            const galleryIndex = Array.from(document.querySelectorAll('#hp2-view-drawer [data-drawer-lightbox-src]'))
              .findIndex((node) => node === lightboxTrigger);
            openDrawerLightbox(
              lightboxTrigger.dataset.drawerLightboxSrc || '',
              lightboxTrigger.dataset.drawerLightboxLabel || 'Drawer media',
              { gallery, index: galleryIndex },
            );
            return;
          }
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
    loadMacrosLocal();
    bindEvents();
    syncSharedControls();
    syncDrawerMediaFilterControls();
    setQuickTapTarget('lovense');
    renderQuickTapActionButtons();
    syncControlReadouts();
    renderSmsThreadPresetList();
    renderMacroSelect();
    if (state.commands.macros.length) {
      selectMacroById(state.commands.macros[0].id);
    }
    renderSettingsForm();
    applyCommandDefaults();
    renderCommandHistory();
    renderCommandFeedback();
    setMailDetailsVisible(false);
    autoSizeMailComposer();
    renderFreshness();
    setCommandSection(state.commands.activeSection || 'all');
    if (state.telemetry.freshnessTimer) {
      clearInterval(state.telemetry.freshnessTimer);
    }
    state.telemetry.freshnessTimer = setInterval(renderFreshness, 15000);
    startLiveStatusAutoRefresh();
    renderAutoFollowButton();
    applyAdminBulkDeviceControlsAccess();
    const jwt = getJwt();
    if (!jwt) {
      showLogin('');
      return;
    }

    try {
      const me = await apiGet('/api/handler/status');
      state.role = me?.role || state.role || 'handler';
      byId('hp2-role').textContent = String(state.role || 'handler').toUpperCase();
      applyAdminBulkDeviceControlsAccess();
      showApp();
      await loadMacrosFromServer();
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
      await loadMacrosFromServer();
      await hydrateApp();
    }
  }

  boot().catch(() => showLogin('Unable to initialize panel.'));
})();
