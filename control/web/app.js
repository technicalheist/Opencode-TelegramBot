(function () {
  "use strict";

  const POLL_MS = 2000;
  const SAVE_NOTE_MS = 3000;
  const BANNER_MS = 6000;
  const MAX_LOG_NODES = 5000;

  const OPENCODE_CAN_START = new Set(["stopped", "unknown", "unauthorized"]);
  const OPENCODE_CAN_STOP = new Set(["running", "unauthorized"]);
  const BOT_CAN_START = new Set(["stopped", "error"]);
  const BOT_CAN_STOP = new Set(["starting", "running", "error"]);
  const BOT_CAN_RESTART = new Set(["stopped", "running", "error"]);

  const state = {
    status: null,
    statusTimer: 0,
    statusInFlight: false,
    statusGeneration: 0,
    actionBusy: false,
    activeTab: "dashboard",
    settings: null,
    settingsLoading: false,
    settingsBusy: false,
    fieldMeta: new Map(),
    inputs: new Map(),
    baseline: {},
    touchedSecrets: new Set(),
    saveNoteTimer: 0,
    bannerTimer: 0,
    setupBusy: false,
    logs: {
      started: false,
      paused: false,
      stream: null,
      reconnectPending: false,
      tailSeq: 0,
      offset: null,
    },
  };

  const dom = {};

  function byId(id) {
    return document.getElementById(id);
  }

  function setText(node, text) {
    if (node && node.textContent !== text) node.textContent = text;
  }

  function setHidden(node, hidden) {
    if (node) node.hidden = Boolean(hidden);
  }

  function setTone(node, tone) {
    if (node && node.getAttribute("data-tone") !== tone) node.setAttribute("data-tone", tone);
  }

  function valueOr(value, fallback) {
    return value === null || value === undefined || value === "" ? fallback : String(value);
  }

  function pad2(value) {
    return String(value).padStart(2, "0");
  }

  function formatUptime(value) {
    if (value === null || value === undefined || value === "") return "—";
    const seconds = Number(value);
    if (!Number.isFinite(seconds)) return "—";
    const total = Math.max(0, Math.floor(seconds));
    const days = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const remaining = total % 60;
    if (days > 0) return `${days}d ${pad2(hours)}h`;
    if (hours > 0) return `${hours}h ${pad2(minutes)}m`;
    if (minutes > 0) return `${minutes}m ${pad2(remaining)}s`;
    return `${remaining}s`;
  }

  function versionLabel(value) {
    if (value === null || value === undefined || value === "") return "—";
    const text = String(value);
    return /^v/i.test(text) ? text : `v${text}`;
  }

  function normalizeMessages(value) {
    const items = Array.isArray(value) ? value : value === null || value === undefined ? [] : [value];
    const messages = [];
    items.forEach(function (item) {
      if (typeof item === "string") {
        if (item) messages.push(item);
        return;
      }
      if (item && typeof item.message === "string" && item.message) {
        messages.push(item.message);
        return;
      }
      if (item && typeof item.msg === "string" && item.msg) {
        messages.push(item.msg);
        return;
      }
      if (item !== null && item !== undefined) {
        try {
          messages.push(JSON.stringify(item));
        } catch (error) {
          messages.push(String(item));
        }
      }
    });
    return messages;
  }

  function ApiError(message, info) {
    this.name = "ApiError";
    this.message = message;
    this.status = info.status;
    this.detail = info.detail;
    this.errors = info.errors;
    this.payload = info.payload;
    if (Error.captureStackTrace) Error.captureStackTrace(this, ApiError);
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  function describeError(error) {
    if (!error) return "Something went wrong.";
    if (error.name === "AbortError") return "The panel took too long to respond.";
    if (error instanceof ApiError) {
      if (error.detail) return error.detail;
      if (error.errors && error.errors.length) return error.errors.join(" ");
      return error.message || "The request failed.";
    }
    if (error instanceof TypeError) return "Could not reach the control panel. Check that it is still running.";
    if (typeof error.message === "string" && error.message) return error.message;
    return "Something went wrong.";
  }

  async function request(path, options) {
    const opts = options || {};
    const method = (opts.method || "GET").toUpperCase();
    const headers = { Accept: "application/json" };
    if (method !== "GET") headers["X-OCV"] = "1";
    if (opts.body !== undefined) headers["Content-Type"] = "application/json";

    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const timeout = opts.timeout || 15000;
    const timer = controller ? window.setTimeout(function () { controller.abort(); }, timeout) : 0;
    let response;
    let raw;

    try {
      response = await fetch(path, {
        method: method,
        headers: headers,
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
        cache: "no-store",
        credentials: "same-origin",
        signal: controller ? controller.signal : undefined,
      });
      raw = await response.text();
    } finally {
      if (timer) window.clearTimeout(timer);
    }

    let payload = null;
    if (raw) {
      try {
        payload = JSON.parse(raw);
      } catch (error) {
        payload = raw;
      }
    }

    if (!response.ok) {
      const detail = payload && typeof payload === "object" && typeof payload.detail === "string"
        ? payload.detail
        : typeof payload === "string" && payload.trim()
          ? payload.trim().slice(0, 400)
          : "";
      const errors = payload && typeof payload === "object" ? normalizeMessages(payload.errors) : [];
      const fallback = detail || `Request failed (${response.status} ${response.statusText || "error"}).`;
      throw new ApiError(fallback, {
        status: response.status,
        detail: detail,
        errors: errors,
        payload: payload,
      });
    }

    return payload;
  }

  function showInline(node, message) {
    if (!node) return;
    node.textContent = message;
    node.hidden = false;
  }

  function hideInline(node) {
    if (!node) return;
    node.textContent = "";
    node.hidden = true;
  }

  function showBanner(message, tone, source, autoHide) {
    window.clearTimeout(state.bannerTimer);
    dom.banner.textContent = message;
    dom.banner.dataset.tone = tone || "info";
    dom.banner.dataset.source = source || "app";
    dom.banner.hidden = false;
    const shouldHide = autoHide === undefined ? tone !== "error" : Boolean(autoHide);
    if (shouldHide) {
      const origin = source || "app";
      state.bannerTimer = window.setTimeout(function () {
        if (dom.banner.dataset.source === origin) hideBanner();
      }, BANNER_MS);
    }
  }

  function hideBanner() {
    window.clearTimeout(state.bannerTimer);
    dom.banner.hidden = true;
    dom.banner.textContent = "";
    delete dom.banner.dataset.tone;
    delete dom.banner.dataset.source;
  }

  function hideStatusBanner() {
    if (dom.banner.dataset.source === "status") hideBanner();
  }

  function toneForOpencode(status) {
    if (status === "running") return "ok";
    if (status === "unauthorized") return "warn";
    if (status === "missing") return "err";
    return "idle";
  }

  function toneForBot(status) {
    if (status === "running") return "ok";
    if (status === "starting" || status === "stopping") return "busy";
    if (status === "error") return "err";
    return "idle";
  }

  function setPill(pill, valueNode, text, tone) {
    setTone(pill, tone);
    setText(valueNode, text);
  }

  function renderStatus(data) {
    if (!data || typeof data !== "object") return;
    state.status = data;

    const panel = data.panel && typeof data.panel === "object" ? data.panel : {};
    const bot = data.bot && typeof data.bot === "object" ? data.bot : {};
    const opencode = data.opencode && typeof data.opencode === "object" ? data.opencode : {};

    const openStatus = valueOr(opencode.status, "unknown");
    const botStatus = valueOr(bot.status, "unknown");
    const mode = valueOr(data.mode, "—");

    setText(dom.appVersion, versionLabel(panel.version));
    setPill(dom.pillOpencode, dom.pillOpencodeValue, openStatus, toneForOpencode(openStatus));
    setPill(dom.pillBot, dom.pillBotValue, botStatus, toneForBot(botStatus));
    setPill(dom.pillMode, dom.pillModeValue, mode, mode === "webhook" || mode === "polling" ? "info" : "idle");

    setText(dom.opencodeStatus, openStatus);
    setTone(dom.opencodeBadge, toneForOpencode(openStatus));
    setText(dom.opencodeVersion, valueOr(opencode.version, "—"));
    setText(dom.opencodeUrl, valueOr(opencode.url, "—"));
    setText(dom.opencodeDetail, valueOr(opencode.detail, "—"));

    let hint = "";
    if (openStatus === "missing") {
      hint = "opencode CLI not found. Install opencode, then start the server.";
    } else if (openStatus === "unauthorized") {
      hint = "password mismatch — check OPENCODE_SERVER_PASSWORD and how the server was started.";
    }
    setText(dom.opencodeHint, hint);
    setHidden(dom.opencodeHint, !hint);

    setText(dom.botStatus, botStatus);
    setTone(dom.botBadge, toneForBot(botStatus));
    setText(dom.botPid, bot.pid === null || bot.pid === undefined ? "—" : String(bot.pid));
    setText(dom.botUptime, formatUptime(bot.uptime));
    setText(dom.botDetail, valueOr(bot.detail, "—"));

    setText(dom.panelVersion, valueOr(panel.version, "—"));
    setText(dom.panelPort, panel.port === null || panel.port === undefined ? "—" : String(panel.port));
    setText(dom.panelConfig, valueOr(panel.config_dir, "—"));
    setText(dom.panelEnv, valueOr(panel.env_file, valueOr(data.env_file, "—")));

    updateDashboardButtons();
  }

  function isStatusPayload(data) {
    return Boolean(data && typeof data === "object" && (data.bot || data.opencode || data.panel));
  }

  function updateDashboardButtons() {
    const data = state.status;
    if (!data) return;
    const openStatus = valueOr(data.opencode && data.opencode.status, "unknown");
    const botStatus = valueOr(data.bot && data.bot.status, "unknown");
    const busy = state.actionBusy;

    dom.opencodeStart.disabled = busy || !OPENCODE_CAN_START.has(openStatus);
    dom.opencodeStop.disabled = busy || !OPENCODE_CAN_STOP.has(openStatus);
    dom.botStart.disabled = busy || !BOT_CAN_START.has(botStatus);
    dom.botStop.disabled = busy || !BOT_CAN_STOP.has(botStatus);
    dom.botRestart.disabled = busy || !BOT_CAN_RESTART.has(botStatus);
  }

  function resetDashboardLabels() {
    setText(dom.opencodeStart, "Start");
    setText(dom.opencodeStop, "Stop");
    setText(dom.botStart, "Start");
    setText(dom.botStop, "Stop");
    setText(dom.botRestart, "Restart");
    dom.dashboardButtons.forEach(function (button) {
      delete button.dataset.busy;
      button.removeAttribute("aria-busy");
    });
  }

  async function refreshStatus() {
    if (state.statusInFlight || state.actionBusy) return;
    state.statusInFlight = true;
    const generation = state.statusGeneration;
    try {
      const data = await request("/api/status", { timeout: 8000 });
      if (generation !== state.statusGeneration) return;
      if (data && typeof data === "object") {
        renderStatus(data);
        hideStatusBanner();
      }
    } catch (error) {
      if (generation !== state.statusGeneration) return;
      showBanner(`Status unavailable: ${describeError(error)}`, "error", "status", false);
    } finally {
      state.statusInFlight = false;
    }
  }

  function scheduleStatusPoll() {
    window.clearTimeout(state.statusTimer);
    state.statusTimer = 0;
    if (document.hidden) return;
    state.statusTimer = window.setTimeout(async function () {
      await refreshStatus();
      scheduleStatusPoll();
    }, POLL_MS);
  }

  function startStatusPolling() {
    void refreshStatus();
    scheduleStatusPoll();
  }

  function handleVisibilityChange() {
    if (document.hidden) {
      window.clearTimeout(state.statusTimer);
      state.statusTimer = 0;
      return;
    }
    void refreshStatus();
    scheduleStatusPoll();
  }

  async function runServiceAction(service, action, button, busyLabel) {
    if (state.actionBusy) return;
    state.actionBusy = true;
    state.statusGeneration += 1;
    window.clearTimeout(state.statusTimer);
    state.statusTimer = 0;

    const errorNode = service === "opencode" ? dom.opencodeError : dom.botError;
    hideInline(errorNode);
    button.textContent = busyLabel;
    button.dataset.busy = "true";
    button.setAttribute("aria-busy", "true");
    updateDashboardButtons();

    let gotStatus = false;
    try {
      const data = await request(`/api/${service}/${action}`, { method: "POST", timeout: 30000 });
      if (isStatusPayload(data)) {
        renderStatus(data);
        gotStatus = true;
      }
    } catch (error) {
      showInline(errorNode, describeError(error));
    } finally {
      state.actionBusy = false;
      resetDashboardLabels();
      updateDashboardButtons();
      scheduleStatusPoll();
      if (!gotStatus) void refreshStatus();
    }
  }

  function cacheDom() {
    dom.appVersion = byId("app-version");
    dom.pillOpencode = byId("pill-opencode");
    dom.pillOpencodeValue = byId("pill-opencode-value");
    dom.pillBot = byId("pill-bot");
    dom.pillBotValue = byId("pill-bot-value");
    dom.pillMode = byId("pill-mode");
    dom.pillModeValue = byId("pill-mode-value");
    dom.banner = byId("banner");
    dom.tablist = document.querySelector('[role="tablist"]');
    dom.tabs = Array.prototype.slice.call(document.querySelectorAll('[role="tab"]'));

    dom.opencodeBadge = byId("opencode-badge");
    dom.opencodeStatus = byId("opencode-status");
    dom.opencodeVersion = byId("opencode-version");
    dom.opencodeUrl = byId("opencode-url");
    dom.opencodeDetail = byId("opencode-detail");
    dom.opencodeHint = byId("opencode-hint");
    dom.opencodeStart = byId("opencode-start");
    dom.opencodeStop = byId("opencode-stop");
    dom.opencodeError = byId("opencode-error");

    dom.botBadge = byId("bot-badge");
    dom.botStatus = byId("bot-status");
    dom.botPid = byId("bot-pid");
    dom.botUptime = byId("bot-uptime");
    dom.botDetail = byId("bot-detail");
    dom.botStart = byId("bot-start");
    dom.botStop = byId("bot-stop");
    dom.botRestart = byId("bot-restart");
    dom.botError = byId("bot-error");

    dom.panelVersion = byId("panel-version");
    dom.panelPort = byId("panel-port");
    dom.panelConfig = byId("panel-config");
    dom.panelEnv = byId("panel-env");

    dom.settingsPath = byId("settings-path");
    dom.settingsLoadError = byId("settings-load-error");
    dom.settingsWarnings = byId("settings-warnings");
    dom.settingsWarningList = byId("settings-warning-list");
    dom.settingsForm = byId("settings-form");
    dom.settingsGroups = byId("settings-groups");
    dom.settingsDirty = byId("settings-dirty");
    dom.settingsNote = byId("settings-note");
    dom.settingsReset = byId("settings-reset");
    dom.settingsSave = byId("settings-save");
    dom.settingsSaveRestart = byId("settings-save-restart");
    dom.settingsErrors = byId("settings-errors");
    dom.settingsErrorList = byId("settings-error-list");

    dom.logSource = byId("log-source");
    dom.logAutoscroll = byId("log-autoscroll");
    dom.logPause = byId("log-pause");
    dom.logClear = byId("log-clear");
    dom.logState = byId("log-state");
    dom.logOutput = byId("log-output");
    dom.logError = byId("log-error");

    dom.setupDialog = byId("setup-dialog");
    dom.setupForm = byId("setup-form");
    dom.setupToken = byId("setup-token");
    dom.setupAdmin = byId("setup-admin");
    dom.setupUrl = byId("setup-url");
    dom.setupPassword = byId("setup-password");
    dom.setupErrors = byId("setup-errors");
    dom.setupErrorList = byId("setup-error-list");
    dom.setupLater = byId("setup-later");
    dom.setupSubmit = byId("setup-submit");

    dom.dashboardButtons = [
      dom.opencodeStart,
      dom.opencodeStop,
      dom.botStart,
      dom.botStop,
      dom.botRestart,
    ];
  }

  function activateTab(tab) {
    if (!tab) return;
    dom.tabs.forEach(function (item) {
      const selected = item === tab;
      item.setAttribute("aria-selected", String(selected));
      item.tabIndex = selected ? 0 : -1;
      const panel = byId(item.getAttribute("aria-controls"));
      if (panel) panel.hidden = !selected;
    });
    state.activeTab = (tab.id || "").replace(/^tab-/, "");
    if (state.activeTab === "settings" && !state.settings && !state.settingsLoading) {
      void loadSettings();
    }
    if (state.activeTab === "logs") ensureLogs();
  }

  function bindTabs() {
    dom.tabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        activateTab(tab);
      });
    });

    if (!dom.tablist) return;
    dom.tablist.addEventListener("keydown", function (event) {
      const current = dom.tabs.indexOf(document.activeElement);
      if (current === -1) return;
      let next = null;
      if (event.key === "ArrowRight") next = (current + 1) % dom.tabs.length;
      else if (event.key === "ArrowLeft") next = (current - 1 + dom.tabs.length) % dom.tabs.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = dom.tabs.length - 1;
      if (next === null) return;
      event.preventDefault();
      const target = dom.tabs[next];
      target.focus();
      activateTab(target);
    });
  }

  function bindDashboard() {
    dom.opencodeStart.addEventListener("click", function () {
      void runServiceAction("opencode", "start", dom.opencodeStart, "Starting…");
    });
    dom.opencodeStop.addEventListener("click", function () {
      void runServiceAction("opencode", "stop", dom.opencodeStop, "Stopping…");
    });
    dom.botStart.addEventListener("click", function () {
      void runServiceAction("bot", "start", dom.botStart, "Starting…");
    });
    dom.botStop.addEventListener("click", function () {
      void runServiceAction("bot", "stop", dom.botStop, "Stopping…");
    });
    dom.botRestart.addEventListener("click", function () {
      void runServiceAction("bot", "restart", dom.botRestart, "Restarting…");
    });
  }

  function fieldIsSecret(field) {
    return field.kind === "secret" || field.secret === true;
  }

  function choiceParts(choice) {
    if (choice && typeof choice === "object") {
      return {
        value: String(choice.value !== undefined ? choice.value : choice.key !== undefined ? choice.key : ""),
        label: String(choice.label !== undefined ? choice.label : choice.value !== undefined ? choice.value : ""),
      };
    }
    return { value: String(choice), label: String(choice) };
  }

  function buildField(field, values, secretsSet) {
    const key = String(field.key);
    const id = `setting-${key.replace(/[^A-Za-z0-9_-]/g, "-")}`;
    const secret = fieldIsSecret(field);
    const current = values[key];
    const initial = secret || current === null || current === undefined ? "" : String(current);

    const wrapper = document.createElement("div");
    wrapper.className = "field";
    wrapper.dataset.key = key;

    const label = document.createElement("label");
    label.className = "field-label";
    label.htmlFor = id;
    label.textContent = field.label || key;
    if (field.required) {
      const star = document.createElement("span");
      star.className = "required";
      star.setAttribute("aria-hidden", "true");
      star.textContent = " *";
      label.appendChild(star);
      const spoken = document.createElement("span");
      spoken.className = "sr-only";
      spoken.textContent = " (required)";
      label.appendChild(spoken);
    }
    wrapper.appendChild(label);

    let input;
    if (field.kind === "choice") {
      input = document.createElement("select");
      if (!field.required) {
        input.appendChild(new Option("—", ""));
      }
      const choices = Array.isArray(field.choices) ? field.choices.map(choiceParts).filter(function (choice) {
        return choice.value !== "";
      }) : [];
      choices.forEach(function (choice) {
        input.appendChild(new Option(choice.label, choice.value));
      });
      if (initial && !choices.some(function (choice) { return choice.value === initial; })) {
        input.appendChild(new Option(initial, initial));
      }
      input.value = initial;
    } else {
      input = document.createElement("input");
      if (secret) {
        input.type = "password";
        input.autocomplete = "new-password";
        input.placeholder = secretsSet[key] ? "•••••• stored" : "";
      } else {
        if (field.kind === "int") {
          input.type = "number";
          input.step = "1";
          input.inputMode = "numeric";
        } else if (field.kind === "url") {
          input.type = "url";
        } else {
          input.type = "text";
        }
        if (typeof field.placeholder === "string" && field.placeholder) input.placeholder = field.placeholder;
        input.value = initial;
        input.autocomplete = "off";
        input.spellcheck = false;
      }
      if (secret) input.value = "";
    }

    input.id = id;
    input.name = key;
    input.className = "field-input";
    if (field.required) input.setAttribute("aria-required", "true");

    const helpId = `${id}-help`;
    if (typeof field.help === "string" && field.help) {
      input.setAttribute("aria-describedby", helpId);
    }

    if (secret) {
      const row = document.createElement("div");
      row.className = "input-row";
      row.appendChild(input);

      const reveal = document.createElement("button");
      reveal.type = "button";
      reveal.className = "btn ghost small";
      reveal.textContent = "Reveal";
      reveal.setAttribute("aria-pressed", "false");
      reveal.setAttribute("aria-controls", id);
      reveal.setAttribute("aria-label", `Reveal ${field.label || key}`);
      reveal.addEventListener("click", function () {
        const showing = input.type === "password";
        input.type = showing ? "text" : "password";
        reveal.textContent = showing ? "Hide" : "Reveal";
        reveal.setAttribute("aria-pressed", String(showing));
        reveal.setAttribute("aria-label", `${showing ? "Hide" : "Reveal"} ${field.label || key}`);
        input.focus();
      });
      row.appendChild(reveal);

      const clear = document.createElement("button");
      clear.type = "button";
      clear.className = "btn ghost small";
      clear.textContent = "Clear";
      clear.setAttribute("aria-controls", id);
      clear.setAttribute("aria-label", `Clear stored ${field.label || key}`);
      clear.disabled = !secretsSet[key];
      clear.addEventListener("click", function () {
        input.value = "";
        input.dataset.cleared = "true";
        state.touchedSecrets.add(key);
        clear.disabled = true;
        updateDirtyState();
        input.focus();
      });
      row.appendChild(clear);
      wrapper.appendChild(row);
    } else {
      wrapper.appendChild(input);
    }

    if (typeof field.help === "string" && field.help) {
      const help = document.createElement("p");
      help.className = "field-help";
      help.id = helpId;
      help.textContent = field.help;
      wrapper.appendChild(help);
    }

    state.fieldMeta.set(key, field);
    state.inputs.set(key, input);
    state.baseline[key] = input.value;
    return wrapper;
  }

  function renderWarnings(warnings) {
    const messages = normalizeMessages(warnings);
    dom.settingsWarningList.textContent = "";
    messages.forEach(function (message) {
      const item = document.createElement("li");
      item.textContent = message;
      dom.settingsWarningList.appendChild(item);
    });
    setHidden(dom.settingsWarnings, messages.length === 0);
  }

  function hideSettingsErrors() {
    if (!dom.settingsErrors) return;
    dom.settingsErrorList.textContent = "";
    dom.settingsErrors.hidden = true;
  }

  function showSettingsErrors(messages) {
    const list = normalizeMessages(messages);
    dom.settingsErrorList.textContent = "";
    if (!list.length) {
      hideSettingsErrors();
      return;
    }
    list.forEach(function (message) {
      const item = document.createElement("li");
      item.textContent = message;
      dom.settingsErrorList.appendChild(item);
    });
    dom.settingsErrors.hidden = false;
  }

  function collectEdits() {
    const edits = {};
    if (!state.settings) return edits;
    state.inputs.forEach(function (input, key) {
      const field = state.fieldMeta.get(key);
      if (!field) return;
      if (fieldIsSecret(field)) {
        // Only send a secret the user actually typed (or explicitly cleared);
        // a touched-then-erased field must not wipe the stored secret.
        if (state.touchedSecrets.has(key)) {
          const cleared = input.dataset.cleared === "true";
          if (input.value !== "" || cleared) edits[key] = input.value;
        }
      } else if (input.value !== state.baseline[key]) {
        edits[key] = input.value;
      }
    });
    return edits;
  }

  function updateDirtyState() {
    const edits = collectEdits();
    const count = Object.keys(edits).length;
    const loaded = Boolean(state.settings);
    setText(dom.settingsDirty, count === 0 ? "No changes" : `${count} unsaved change${count === 1 ? "" : "s"}`);
    dom.settingsDirty.dataset.dirty = count > 0 ? "true" : "false";
    dom.settingsSave.disabled = state.settingsBusy || !loaded || count === 0;
    dom.settingsReset.disabled = state.settingsBusy || !loaded || count === 0;
    dom.settingsSaveRestart.disabled = state.settingsBusy || !loaded;
    return count;
  }

  function clearSaveNote() {
    window.clearTimeout(state.saveNoteTimer);
    setText(dom.settingsNote, "");
  }

  function showSaveNote(text) {
    window.clearTimeout(state.saveNoteTimer);
    setText(dom.settingsNote, text);
    state.saveNoteTimer = window.setTimeout(function () {
      if (dom.settingsNote.textContent === text) setText(dom.settingsNote, "");
    }, SAVE_NOTE_MS);
  }

  function setSettingsControlsDisabled(disabled) {
    if (!dom.settingsGroups) return;
    dom.settingsGroups.querySelectorAll("input, select, button").forEach(function (control) {
      control.disabled = disabled;
    });
  }

  function setSettingsBusy(busy, button, label) {
    state.settingsBusy = Boolean(busy);
    setText(dom.settingsSave, "Save");
    setText(dom.settingsSaveRestart, "Save & restart bot");
    dom.settingsSave.removeAttribute("aria-busy");
    dom.settingsSaveRestart.removeAttribute("aria-busy");
    delete dom.settingsSave.dataset.busy;
    delete dom.settingsSaveRestart.dataset.busy;
    if (busy && button) {
      button.textContent = label;
      button.dataset.busy = "true";
      button.setAttribute("aria-busy", "true");
    }
    if (busy) {
      dom.settingsForm.setAttribute("aria-busy", "true");
    } else {
      dom.settingsForm.removeAttribute("aria-busy");
    }
    setSettingsControlsDisabled(Boolean(busy));
    updateDirtyState();
  }

  function renderSettings(data) {
    state.settings = data;
    state.fieldMeta.clear();
    state.inputs.clear();
    state.baseline = {};
    state.touchedSecrets.clear();

    const values = data.values && typeof data.values === "object" ? data.values : {};
    const secretsSet = data.secrets_set && typeof data.secrets_set === "object" ? data.secrets_set : {};
    const fields = Array.isArray(data.fields) ? data.fields.filter(function (field) {
      return field && field.key !== null && field.key !== undefined;
    }) : [];
    const groups = Array.isArray(data.groups) && data.groups.length
      ? data.groups.map(String)
      : [];

    setText(dom.settingsPath, valueOr(data.env_file, "—"));
    hideInline(dom.settingsLoadError);
    renderWarnings(data.warnings);

    dom.settingsGroups.textContent = "";
    const byGroup = new Map();
    groups.forEach(function (group) {
      byGroup.set(group, []);
    });
    fields.forEach(function (field) {
      const group = field.group ? String(field.group) : "Other";
      if (!byGroup.has(group)) byGroup.set(group, []);
      byGroup.get(group).push(field);
    });

    const fragment = document.createDocumentFragment();
    byGroup.forEach(function (items, group) {
      if (!items.length) return;
      const section = document.createElement("section");
      section.className = "card settings-group";

      const heading = document.createElement("h3");
      heading.className = "settings-group-title";
      heading.textContent = group;
      section.appendChild(heading);

      const grid = document.createElement("div");
      grid.className = "settings-fields";
      items.forEach(function (field) {
        grid.appendChild(buildField(field, values, secretsSet));
      });
      section.appendChild(grid);
      fragment.appendChild(section);
    });

    if (!fields.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "No settings fields are available.";
      fragment.appendChild(empty);
    }

    dom.settingsGroups.appendChild(fragment);
    updateDirtyState();
  }

  async function loadSettings() {
    if (state.settingsLoading) return state.settings;
    state.settingsLoading = true;
    hideInline(dom.settingsLoadError);
    try {
      const data = await request("/api/settings", { timeout: 10000 });
      if (!data || typeof data !== "object") throw new Error("The panel returned an unexpected settings response.");
      renderSettings(data);
      return data;
    } catch (error) {
      showInline(dom.settingsLoadError, `Could not load settings: ${describeError(error)}`);
      if (!state.settings && dom.settingsGroups.childElementCount === 0) {
        const empty = document.createElement("p");
        empty.className = "empty-state";
        empty.textContent = "Settings are unavailable until the panel API responds. Switch tabs to retry.";
        dom.settingsGroups.appendChild(empty);
      }
      updateDirtyState();
      return null;
    } finally {
      state.settingsLoading = false;
    }
  }

  function onSettingsEdit(event) {
    const target = event.target;
    if (!target || !target.classList || !target.classList.contains("field-input")) return;
    const wrapper = target.closest("[data-key]");
    if (!wrapper) return;
    const key = wrapper.dataset.key;
    const field = state.fieldMeta.get(key);
    if (!field) return;
    if (fieldIsSecret(field)) {
      state.touchedSecrets.add(key);
      if (target.value !== "") target.dataset.cleared = "false";
    }
    hideSettingsErrors();
    clearSaveNote();
    updateDirtyState();
  }

  async function saveSettings(restart) {
    if (state.settingsBusy || !state.settings) return;
    const values = collectEdits();
    if (!restart && Object.keys(values).length === 0) return;

    hideSettingsErrors();
    clearSaveNote();
    setSettingsBusy(true, restart ? dom.settingsSaveRestart : dom.settingsSave, restart ? "Saving & restarting…" : "Saving…");

    try {
      const data = await request("/api/settings", {
        method: "PUT",
        timeout: 30000,
        body: { values: values, restart: Boolean(restart) },
      });
      if (!data || typeof data !== "object") throw new Error("The panel returned an unexpected settings response.");
      renderSettings(data);
      const restartError = typeof data.restart_error === "string" ? data.restart_error : "";
      showSaveNote("Saved.");
      if (restartError) showSettingsErrors([restartError]);
      void refreshStatus();
    } catch (error) {
      const errors = error instanceof ApiError && error.errors && error.errors.length
        ? error.errors
        : [describeError(error)];
      showSettingsErrors(errors);
    } finally {
      setSettingsBusy(false);
    }
  }

  function resetSettings() {
    if (!state.settings) return;
    renderSettings(state.settings);
    hideSettingsErrors();
    showSaveNote("Changes reset.");
  }

  function bindSettings() {
    dom.settingsForm.addEventListener("submit", function (event) {
      event.preventDefault();
      void saveSettings(false);
    });
    dom.settingsSaveRestart.addEventListener("click", function () {
      void saveSettings(true);
    });
    dom.settingsReset.addEventListener("click", resetSettings);
    dom.settingsGroups.addEventListener("input", onSettingsEdit);
    dom.settingsGroups.addEventListener("change", onSettingsEdit);
  }

  function setLogState(text, tone) {
    setText(dom.logState, text);
    setTone(dom.logState, tone || "idle");
  }

  function showLogError(message) {
    showInline(dom.logError, message);
  }

  function hideLogError() {
    hideInline(dom.logError);
  }

  function scrollLogsToBottom() {
    dom.logOutput.scrollTop = dom.logOutput.scrollHeight;
  }

  function appendLogLine(line) {
    const text = String(line);
    const suffix = text.endsWith("\n") ? "" : "\n";
    dom.logOutput.appendChild(document.createTextNode(text + suffix));
    while (dom.logOutput.childNodes.length > MAX_LOG_NODES) {
      dom.logOutput.removeChild(dom.logOutput.firstChild);
    }
    if (dom.logAutoscroll.checked) scrollLogsToBottom();
  }

  async function loadLogTail(options) {
    const replace = Boolean(options && options.replace);
    const source = dom.logSource.value;
    const seq = ++state.logs.tailSeq;
    try {
      const data = await request(`/api/logs?source=${encodeURIComponent(source)}&lines=200`, { timeout: 10000 });
      if (seq !== state.logs.tailSeq || dom.logSource.value !== source) return;
      hideLogError();
      if (replace) dom.logOutput.textContent = "";
      if (data && typeof data.offset === "number") state.logs.offset = data.offset;
      const lines = data && Array.isArray(data.lines) ? data.lines : [];
      lines.forEach(function (line) {
        appendLogLine(typeof line === "string" ? line : String(line));
      });
      if (dom.logAutoscroll.checked) scrollLogsToBottom();
    } catch (error) {
      if (seq !== state.logs.tailSeq) return;
      showLogError(`Could not load logs: ${describeError(error)}`);
    }
  }

  function closeLogStream() {
    const stream = state.logs.stream;
    state.logs.stream = null;
    state.logs.reconnectPending = false;
    if (stream) {
      try {
        stream.close();
      } catch (error) {
        /* already closed */
      }
    }
  }

  function connectLogs() {
    if (state.logs.paused) return;
    if (typeof EventSource !== "function") {
      setLogState("Unavailable", "err");
      showLogError("Live logs are not supported in this browser.");
      return;
    }

    closeLogStream();
    const source = dom.logSource.value;
    setLogState("Connecting…", "busy");

    try {
      const offset = Number.isFinite(state.logs.offset) ? state.logs.offset : 0;
      const stream = new EventSource(`/api/logs/stream?source=${encodeURIComponent(source)}&offset=${offset}`);
      state.logs.stream = stream;

      stream.addEventListener("line", function (event) {
        if (state.logs.stream !== stream || state.logs.paused) return;
        let line;
        try {
          line = JSON.parse(event.data);
        } catch (error) {
          line = event.data;
        }
        appendLogLine(typeof line === "string" ? line : String(line));
      });

      stream.onopen = function () {
        if (state.logs.stream !== stream) return;
        const reconnected = state.logs.reconnectPending;
        state.logs.reconnectPending = false;
        hideLogError();
        setLogState("Live", "ok");
        if (reconnected) void loadLogTail({ replace: true });
      };

      stream.onerror = function () {
        if (state.logs.stream !== stream) return;
        state.logs.reconnectPending = true;
        if (stream.readyState === EventSource.CLOSED) {
          setLogState("Disconnected", "err");
          showLogError("The log stream closed unexpectedly. Reopen the Logs tab to reconnect.");
        } else {
          setLogState("Reconnecting…", "busy");
        }
      };
    } catch (error) {
      setLogState("Disconnected", "err");
      showLogError(`Could not open the log stream: ${describeError(error)}`);
    }
  }

  async function startLogs() {
    setLogState("Loading…", "busy");
    await loadLogTail({ replace: true });
    if (!state.logs.paused) connectLogs();
  }

  function ensureLogs() {
    if (!state.logs.started) {
      state.logs.started = true;
      void startLogs();
      return;
    }
    if (!state.logs.paused && (!state.logs.stream
        || (typeof EventSource === "function" && state.logs.stream.readyState === EventSource.CLOSED))) {
      void loadLogTail({ replace: true });
      connectLogs();
    }
  }

  async function changeLogSource() {
    if (!state.logs.started) return;
    const source = dom.logSource.value;
    closeLogStream();
    hideLogError();
    dom.logOutput.textContent = "";

    if (state.logs.paused) {
      setLogState("Paused", "idle");
      await loadLogTail({ replace: true });
      return;
    }

    setLogState("Loading…", "busy");
    await loadLogTail({ replace: true });
    if (!state.logs.paused && dom.logSource.value === source) connectLogs();
  }

  async function toggleLogPause() {
    if (!state.logs.started) {
      ensureLogs();
      return;
    }
    state.logs.paused = !state.logs.paused;
    dom.logPause.setAttribute("aria-pressed", String(state.logs.paused));
    dom.logPause.textContent = state.logs.paused ? "Resume stream" : "Pause stream";

    if (state.logs.paused) {
      closeLogStream();
      setLogState("Paused", "idle");
      return;
    }

    setLogState("Loading…", "busy");
    await loadLogTail({ replace: true });
    if (!state.logs.paused) connectLogs();
  }

  function bindLogs() {
    dom.logSource.addEventListener("change", function () {
      void changeLogSource();
    });
    dom.logPause.addEventListener("click", function () {
      void toggleLogPause();
    });
    dom.logClear.addEventListener("click", function () {
      dom.logOutput.textContent = "";
    });
    dom.logAutoscroll.addEventListener("change", function () {
      if (dom.logAutoscroll.checked) scrollLogsToBottom();
    });
  }

  function hideSetupErrors() {
    dom.setupErrorList.textContent = "";
    dom.setupErrors.hidden = true;
  }

  function showSetupErrors(messages) {
    const list = normalizeMessages(messages);
    dom.setupErrorList.textContent = "";
    list.forEach(function (message) {
      const item = document.createElement("li");
      item.textContent = message;
      dom.setupErrorList.appendChild(item);
    });
    dom.setupErrors.hidden = list.length === 0;
    if (list.length) dom.setupErrors.focus();
  }

  function setSetupBusy(busy) {
    state.setupBusy = Boolean(busy);
    dom.setupForm.querySelectorAll("input").forEach(function (input) {
      input.disabled = Boolean(busy);
    });
    dom.setupLater.disabled = Boolean(busy);
    dom.setupSubmit.disabled = Boolean(busy);
    setText(dom.setupSubmit, busy ? "Setting up…" : "Save & start bot");
    if (busy) {
      dom.setupSubmit.dataset.busy = "true";
      dom.setupSubmit.setAttribute("aria-busy", "true");
    } else {
      delete dom.setupSubmit.dataset.busy;
      dom.setupSubmit.removeAttribute("aria-busy");
    }
  }

  function closeSetupDialog() {
    if (typeof dom.setupDialog.close === "function" && dom.setupDialog.open) {
      dom.setupDialog.close();
    } else {
      dom.setupDialog.removeAttribute("open");
    }
  }

  function prefillSetup() {
    const values = state.settings && state.settings.values && typeof state.settings.values === "object"
      ? state.settings.values
      : {};
    dom.setupToken.value = "";
    dom.setupAdmin.value = valueOr(values.ADMIN_USER_ID, "");
    dom.setupUrl.value = valueOr(values.OPENCODE_BASE_URL, "http://localhost:4096");
    dom.setupPassword.value = "";
  }

  async function openSetupIfNeeded(settingsPromise) {
    let setup;
    try {
      setup = await request("/api/setup", { timeout: 10000 });
    } catch (error) {
      showBanner(`Could not check first-run setup: ${describeError(error)}`, "warn", "setup", true);
      return;
    }
    if (!setup || !setup.needed) return;
    await settingsPromise;
    if (dom.setupDialog.open) return;
    prefillSetup();
    hideSetupErrors();
    setSetupBusy(false);
    if (typeof dom.setupDialog.showModal === "function") {
      try {
        dom.setupDialog.showModal();
      } catch (error) {
        dom.setupDialog.setAttribute("open", "");
      }
    } else {
      dom.setupDialog.setAttribute("open", "");
    }
  }

  async function submitSetup(event) {
    event.preventDefault();
    if (state.setupBusy) return;

    hideSetupErrors();
    setSetupBusy(true);

    const values = {
      TELEGRAM_BOT_TOKEN: dom.setupToken.value.trim(),
      ADMIN_USER_ID: dom.setupAdmin.value.trim(),
      OPENCODE_BASE_URL: dom.setupUrl.value.trim(),
    };
    const password = dom.setupPassword.value;
    if (password.trim()) values.OPENCODE_SERVER_PASSWORD = password;

    try {
      const data = await request("/api/setup", {
        method: "POST",
        timeout: 30000,
        body: { values: values, start: true },
      });
      closeSetupDialog();
      if (data && typeof data.start_error === "string" && data.start_error) {
        showBanner(`Setup saved, but the bot did not start: ${data.start_error}`, "warn", "setup", true);
      } else if (data && data.started) {
        showBanner("Setup complete. Starting the bot…", "ok", "setup", true);
      } else {
        showBanner("Setup complete.", "ok", "setup", true);
      }
      void loadSettings();
      void refreshStatus();
    } catch (error) {
      const errors = error instanceof ApiError && error.errors && error.errors.length
        ? error.errors
        : [describeError(error)];
      showSetupErrors(errors);
    } finally {
      setSetupBusy(false);
    }
  }

  function bindSetup() {
    dom.setupForm.addEventListener("submit", function (event) {
      void submitSetup(event);
    });
    dom.setupLater.addEventListener("click", closeSetupDialog);
    dom.setupDialog.addEventListener("cancel", function (event) {
      if (state.setupBusy) event.preventDefault();
    });
  }

  function init() {
    cacheDom();
    bindTabs();
    bindDashboard();
    bindSettings();
    bindLogs();
    bindSetup();

    startStatusPolling();
    const settingsPromise = loadSettings();
    void openSetupIfNeeded(settingsPromise);

    document.addEventListener("visibilitychange", handleVisibilityChange);
    window.addEventListener("beforeunload", closeLogStream);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
