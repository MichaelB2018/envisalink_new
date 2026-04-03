/**
 * app.js — core application controller
 *
 * Responsibilities:
 *  - Determine base path from <base> tag (ingress support)
 *  - WebSocket connection to /ws with auto-reconnect
 *  - Update the retro LCD display widget on every panel display push
 *  - Manage the two-stage UI reveal (cold-start → config loaded)
 *  - Installer code modal logic
 *  - Tab switching
 *  - Toast notification helper (window.toast)
 *  - Shared API helper (window.api)
 */

"use strict";

/* ── Globals populated on DOMReady ── */
window.APP = {
  basePath: "",
  ws: null,
  config: null,           // last loaded config cache
  codeSet: false,
  userCode: "",           // user/disarm code (populated from hint endpoint)
  connMode: "ha",         // "ha" or "direct" — updated by settings.js
};

/* ═══════════════════════════════════════════════════════════════════════
   Helpers
═══════════════════════════════════════════════════════════════════════ */

/**
 * Derive base path from the <base> tag injected by the server.
 * HA ingress prepends a path prefix; without it we'd have broken URLs.
 */
function getBasePath() {
  const base = document.getElementById("base-tag");
  if (!base) return "";
  const href = base.getAttribute("href") || "";
  // Strip trailing slash
  return href.endsWith("/") ? href.slice(0, -1) : href;
}

/**
 * Minimal fetch wrapper — always includes credentials and resolves/rejects
 * with parsed JSON (or throws on non-2xx).
 */
window.api = async function api(method, path, body) {
  const url = APP.basePath + path;
  const opts = {
    method,
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
  };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(url, opts);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || res.statusText);
  }
  return res.status === 204 ? null : res.json();
};

/**
 * Toast notification.  type: "ok" | "err" | ""
 */
window.toast = function toast(msg, type = "", duration = 3000) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = `toast ${type}`;
  el.classList.remove("hidden");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.add("hidden"), duration);
};

/** Lock the settings area: show busy overlay over tabs + disable interaction.
 *  The LCD display above remains visible and updating.
 *  Call before any operation that programs the panel. */
window.lockUI = function lockUI(msg) {
  $("#busy-desc").text(msg || "Programming the panel \u2014 please wait.");
  $("#busy-overlay").removeClass("hidden");
  $(".tab-bar .tab-btn").addClass("disabled").prop("disabled", true);
  $(".tab-panel").addClass("tab-disabled");
  // Scroll the active tab panel to top (page itself doesn't scroll)
  $(".tab-panel.active").scrollTop(0);
};

/** Unlock: hide busy overlay, re-enable all tabs. */
window.unlockUI = function unlockUI() {
  $("#busy-overlay").addClass("hidden");
  $(".tab-bar .tab-btn").removeClass("disabled").prop("disabled", false);
  $(".tab-panel").removeClass("tab-disabled");
};

/* ═══════════════════════════════════════════════════════════════════════
   LCD display
═══════════════════════════════════════════════════════════════════════ */

// Vista 20P has a 16-character × 2-line display
const LCD_COLS = 16;

function setLcdDisplay(text) {
  let l1, l2;
  const t = text || "";
  if (t.includes("\n")) {
    const parts = t.split("\n");
    l1 = (parts[0] || "").padEnd(LCD_COLS).slice(0, LCD_COLS);
    l2 = (parts[1] || "").padEnd(LCD_COLS).slice(0, LCD_COLS);
  } else {
    // 32-char flat string: first 16 = line 1, next 16 = line 2
    l1 = t.slice(0, 16).padEnd(LCD_COLS);
    l2 = t.slice(16, 32).padEnd(LCD_COLS);
  }
  const $l1 = $("#lcd-line1");
  const $l2 = $("#lcd-line2");
  $l1.text(l1).addClass("flash");
  $l2.text(l2).addClass("flash");
  setTimeout(() => { $l1.removeClass("flash"); $l2.removeClass("flash"); }, 300);
}

/**
 * Update the physical-keypad LED indicator row from the partition status
 * attributes delivered in every WS display event.
 *
 * Source: IconLED_Flags from honeywell_envisalinkdefs.py, exposed by
 * sensor.py as extra_state_attributes → pushed to browser unchanged.
 *
 * Cursor position: NOT available from the Honeywell EVL TPI protocol.
 * The %00 keypad update carries only the 32-char alpha text, no cursor
 * field.  The cursor is a hardware feature of the physical keypad.
 *
 * The PROG (programming mode) LED is heuristic — the panel does not
 * send an explicit flag for it over the EVL TPI.  We detect it by
 * looking for known programming-mode prompt strings in the alpha text.
 */
const PROG_PATTERNS = [
  /^\*\d{2}/,               // starts with *NN (data field display)
  /PROGRAM ALPHA/i,
  /SET TO CONFIRM/i,
  /ENTER ZN NUM/i,
  /CUSTOM WORD/i,
  /CUSTOM\?/i,              // custom word edit prompt (CUSTOM? 01 ...)
  /INSTALLER CODE/i,
  /^\d{2}\s+\d{2}\s+\d/,   // zone type display (ZZ ZT P RC HW:RT)
  /Field\?/i,               // data-field mode prompt
  /Enter \* or #/i,         // navigation prompt inside *58
];

function updateLeds(attrs, displayText) {
  if (!attrs || typeof attrs !== "object") return;

  const armed = !!(attrs.armed_away || attrs.armed_stay || attrs.armed_zero_entry_delay);
  const alarm = !!(attrs.alarm || attrs.alarm_fire_zone);
  const fire  = !!(attrs.fire  || attrs.alarm_fire_zone);

  // PROG: heuristically detected from display text (no protocol flag exists)
  const text = displayText || "";
  const prog = PROG_PATTERNS.some(re => re.test(text));

  const map = [
    // [element-id,  active,           colour-class, flash?]
    ["led-ready",   !!attrs.ready,     "on-green",   false],
    ["led-armed",   armed,             "on-amber",   false],
    ["led-bypass",  !!attrs.armed_bypass, "on-amber", false],
    ["led-trouble", !!attrs.trouble,   "on-red",     false],
    ["led-alarm",   alarm,             alarm ? "flash-red" : "on-red", alarm],
    ["led-fire",    fire,              fire  ? "flash-red" : "on-red", fire],
    ["led-ac",      !!attrs.ac_present, "on-green",  false],
    ["led-chime",   !!attrs.chime,     "on-amber",   false],
    ["led-battery", !!attrs.low_battery, "on-red",   false],
    ["led-prog",    prog,              "on-amber",   false],
  ];

  map.forEach(([id, on, cls, _]) => {
    const $dot = $("#" + id);
    $dot.removeClass("on-green on-amber on-red flash-red");
    if (on) $dot.addClass(cls);
  });

  // Show/hide the "Read Bypass" button based on BYPASS LED state
  $("#btn-bypass-scan").toggleClass("hidden", !attrs.armed_bypass);
}

/* ═══════════════════════════════════════════════════════════════════════
   WebSocket
═══════════════════════════════════════════════════════════════════════ */

let _wsRetryDelay = 2000;
let _wsRetryTimer = null;

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}${APP.basePath}/ws`;
  const ws = new WebSocket(url);
  APP.ws = ws;

  setConnState("connecting");

  ws.onopen = () => {
    _wsRetryDelay = 2000;
    setConnState("connected");
    // Heartbeat ping every 20s
    ws._pingTimer = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ping" }));
    }, 20_000);
  };

  ws.onmessage = (evt) => {
    let msg;
    try { msg = JSON.parse(evt.data); } catch { return; }
    handleWsMessage(msg);
  };

  ws.onerror = () => {};

  ws.onclose = () => {
    clearInterval(ws._pingTimer);
    setConnState("disconnected");
    APP.ws = null;
    _wsRetryTimer = setTimeout(connectWs, _wsRetryDelay);
    _wsRetryDelay = Math.min(_wsRetryDelay * 2, 30_000);
  };
}

function handleWsMessage(msg) {
  switch (msg.type) {
    case "display":
      setLcdDisplay(msg.display || "");
      updateLeds(msg.attributes, msg.display || "");
      $(document).trigger("snifferDisplay", [msg.display || "", msg.attributes]);
      break;

    case "code_status":
      APP.codeSet = msg.set;
      if (!msg.set) {
        showCodeModal();
      } else {
        // Populate APP.userCode from saved hint so other modules can use it
        api("GET", "/api/user_code_hint").then(h => { APP.userCode = h.code || ""; }).catch(() => {});
      }
      break;

    case "scan_started":
      $("#cold-start-hint, #btn-read-config").addClass("hidden");
      $("#scan-progress-panel").removeClass("hidden");
      updateScanProgress(0, 33, "Entering programming mode…");
      $("#scan-badge").removeClass("hidden");
      $("#btn-refresh-config").addClass("spinning");
      $(".btn-section-rescan").prop("disabled", true);
      // If settings are already visible (re-scan), show inline progress & disable tabs
      if ($("#settings-ui").is(":visible")) {
        _rescanMode = true;
        _rescanSection = msg.section || null;
        $("#rescan-error-panel").addClass("hidden");
        $("#rescan-progress-panel").removeClass("hidden");
        const label = _rescanSection
          ? `Re-syncing ${_rescanSection}\u2026`
          : "Re-syncing panel configuration\u2026";
        _updateRescanProgress(0, msg.total || 33, label);
        $(".tab-bar .tab-btn").addClass("disabled").prop("disabled", true);
        $(".tab-panel").addClass("tab-disabled");
      }
      break;

    case "scan_progress":
      updateScanProgress(msg.step, msg.total, msg.msg || "");
      if (_rescanMode) _updateRescanProgress(msg.step, msg.total, msg.msg || "");
      break;

    case "scan_complete":
      $("#scan-progress-panel").addClass("hidden");
      $("#scan-badge").addClass("hidden");
      $("#btn-refresh-config").removeClass("spinning");
      {
        const section = _rescanSection;
        _endRescan();
        // Always re-enable rescan buttons — _endRescan() skips this when
        // _rescanMode is false (initial scan), leaving them stuck disabled.
        $(".btn-section-rescan").prop("disabled", false);
        APP.config = msg.config;
        revealSettingsUi(msg.config);
        toast(section ? `${section} re-synced \u2713` : "Panel config loaded \u2713", "ok");
      }
      break;

    case "scan_error":
      $("#scan-progress-panel").addClass("hidden");
      $("#scan-badge").addClass("hidden");
      $("#btn-refresh-config").removeClass("spinning");
      const wasRescan = _rescanMode;
      _endRescan();
      if (wasRescan) {
        _showRescanError(msg.error);
      } else if (msg.aborted) {
        showScanAbortError(msg.error);
      } else {
        $("#cold-start-hint, #btn-read-config").removeClass("hidden");
        toast(`Scan failed: ${msg.error}`, "err", 5000);
      }
      break;

    case "ha_error":
      showHaError(msg.error);
      break;

    case "evl_status":
      // Fired by EvlClient when the direct TCP connection to the EVL is
      // established or lost.  Forward to settings.js via a custom event.
      $(document).trigger("evlStatusUpdate", [msg.connected]);
      break;

    case "keypress_sent":
      // Fired by server after any route sends keypresses to the panel.
      // Forward to sniffer for capture (source label included for context).
      $(document).trigger("snifferKeypress", [msg.keys || "", msg.source || ""]);
      break;
  }
}

function showScanAbortError(errorMsg) {
  $("#scan-abort-msg").text(errorMsg);
  $("#scan-abort-panel").removeClass("hidden");
  $("#cold-start-hint, #btn-read-config").addClass("hidden");
  $("#scan-debug-panel").addClass("hidden");
  $("#scan-debug-log").text("");
  $("#btn-scan-debug-toggle").text("Show debug log");
  // "Try again" — hide the abort panel and re-show the normal start button
  $("#btn-scan-retry").off("click").on("click", function () {
    $("#scan-abort-panel").addClass("hidden");
    $("#cold-start-hint, #btn-read-config").removeClass("hidden");
  });
  // Toggle debug log visibility and lazy-load entries
  $("#btn-scan-debug-toggle").off("click").on("click", async function () {
    const $panel = $("#scan-debug-panel");
    if ($panel.is(":visible")) {
      $panel.addClass("hidden");
      $(this).text("Show debug log");
      return;
    }
    $(this).text("Loading\u2026");
    try {
      const data = await api("GET", "/api/scan_log");
      const lines = (data.entries || []).map(e => {
        const ts = new Date(e.t * 1000).toISOString().substr(11, 12);
        const flag = e.level === "error" ? "\u274C" : e.level === "warn" ? "\u26A0\uFE0F" : "\u2713";
        let line = `[${ts}] ${flag} ${e.step}`;
        if (e.keys)    line += `\n  keys:    ${e.keys}`;
        if (e.display) line += `\n  display: ${JSON.stringify(e.display)}`;
        if (e.note)    line += `\n  note:    ${e.note}`;
        return line;
      }).join("\n\n");
      $("#scan-debug-log").text(lines || "(no entries)");
    } catch (err) {
      $("#scan-debug-log").text(`Failed to load: ${err.message}`);
    }
    $panel.removeClass("hidden");
    $(this).text("Hide debug log");
  });
}

function showHaError(msg) {  // Persistent banner — replaces cold-start hint so it's impossible to miss
  $("#cold-start-panel").html(
    `<div class="ha-error-banner">
       <strong>&#9888; Configuration Error</strong><br>
       ${msg}<br><br>
       Go to <strong>Settings &rarr; Add-ons &rarr; Envisalink Programming Interface
       &rarr; Configuration</strong>, correct the entity IDs, then click
       <strong>Restart</strong>.
       <div id="entity-suggestions" style="margin-top:12px"></div>
     </div>`
  ).removeClass("hidden");
  // Hide scan controls — nothing works without valid entities
  $("#btn-rescan, #btn-refresh-config").addClass("hidden");
  toast(msg, "err", 10000);
  // Auto-detect likely entities and offer a one-click fix
  _loadEntitySuggestions();
}

async function _loadEntitySuggestions() {
  const $box = $("#entity-suggestions");
  $box.html(`<em style="font-size:0.85em">Searching for envisalink entities&hellip;</em>`);
  let suggestions = { keypad_sensor: [], partition_entity: [] };
  try {
    suggestions = await api("GET", "/api/suggest_entities");
  } catch (e) {
    // Discovery failed — still show the manual-entry form below
  }
  const ks = suggestions.keypad_sensor || [];
  const pe = suggestions.partition_entity || [];

  // For each slot: select with candidates, or plain text input if none found
  function _fieldHtml(id, candidates, placeholder) {
    if (candidates.length) {
      const opts = candidates.map(e => `<option value="${e}">${e}</option>`).join("");
      return `<select id="${id}" style="width:100%;max-width:380px">${opts}</select>`;
    }
    return `<input id="${id}" type="text" placeholder="${placeholder}"
              style="width:100%;max-width:380px;padding:4px 6px;background:#1a1a1a;
                     color:#eee;border:1px solid #555;border-radius:3px;font-size:0.9em">`;
  }

  const heading = (ks.length || pe.length)
    ? "<strong>Detected entities:</strong>"
    : "<strong>No envisalink entities detected &mdash; enter manually:</strong>";

  $box.html(`
    <div style="font-size:0.9em;margin-bottom:8px">${heading}</div>
    <table style="border-collapse:collapse;width:100%;font-size:0.85em">
      <tr>
        <td style="padding:4px 8px 4px 0;white-space:nowrap">Keypad sensor</td>
        <td>${_fieldHtml("suggest-ks", ks, "sensor.my_keypad_sensor")}</td>
      </tr>
      <tr>
        <td style="padding:4px 8px 4px 0;white-space:nowrap">Partition entity</td>
        <td>${_fieldHtml("suggest-pe", pe, "alarm_control_panel.my_partition")}</td>
      </tr>
    </table>
    <button id="btn-apply-entities" class="btn"
      style="margin-top:10px;background:#1976d2;color:#fff;border:none;padding:6px 16px;border-radius:4px;cursor:pointer">
      Save &amp; reconnect
    </button>
    <span id="apply-status" style="margin-left:10px;font-size:0.85em"></span>
  `);

  $("#btn-apply-entities").on("click", async function () {
    const ks_val = ($("#suggest-ks").val() || "").trim();
    const pe_val = ($("#suggest-pe").val() || "").trim();
    if (!ks_val) {
      $("#apply-status").css("color", "#f44336").text("Please enter a keypad sensor entity ID.");
      return;
    }
    if (!pe_val) {
      $("#apply-status").css("color", "#f44336").text("Please enter a partition entity ID.");
      return;
    }
    if (!ks_val.startsWith("sensor.")) {
      $("#apply-status").css("color", "#f44336").text("Keypad sensor must start with sensor.*");
      return;
    }
    if (!pe_val.startsWith("alarm_control_panel.")) {
      $("#apply-status").css("color", "#f44336").text("Partition entity must start with alarm_control_panel.*");
      return;
    }
    $(this).prop("disabled", true).text("Saving\u2026");
    $("#apply-status").text("");
    try {
      await api("POST", "/api/apply_entities", { keypad_sensor: ks_val, partition_entity: pe_val });
      $("#apply-status").css("color", "#4caf50").text("Saved. Reconnecting\u2026");
      $(this).text("Done");
      // Server reinitialises in-process — just reload after a short pause
      setTimeout(() => location.reload(), 2500);
    } catch (e) {
      $("#apply-status").css("color", "#f44336").text(`Failed: ${e.message}`);
      $("#btn-apply-entities").prop("disabled", false).text("Save & reconnect");
    }
  });
}

/**
 * Poll a lightweight endpoint until the restarted add-on responds, then reload.
 * We wait an initial 3 s (give the process time to die), then probe every 2 s
 * for up to 60 s before giving up and asking the user to reload manually.
 */
function _waitForAddonRestart() {
  const MAX_WAIT_MS = 60000;
  const POLL_INTERVAL_MS = 2000;
  const INITIAL_DELAY_MS = 3000;
  const deadline = Date.now() + MAX_WAIT_MS;

  setTimeout(async function poll() {
    try {
      await api("GET", "/api/code/set");
      // Server responded — reload into the freshly-started instance
      location.reload();
    } catch (_) {
      if (Date.now() < deadline) {
        setTimeout(poll, POLL_INTERVAL_MS);
      } else {
        $("#apply-status").css("color", "#f44336")
          .text("Add-on did not come back in time. Please reload manually.");
      }
    }
  }, INITIAL_DELAY_MS);
}

function updateScanProgress(step, total, msg) {
  const pct = total > 0 ? Math.min(100, Math.round((step / total) * 100)) : 0;
  $("#scan-progress-bar").css("width", pct + "%");
  $("#scan-progress-step").text(`Step ${step} of ${total}`);
  if (msg) $("#scan-progress-msg").text(msg);
}

/* Re-sync progress (inline, shown when settings UI is already visible) */
let _rescanMode = false;
let _rescanSection = null;  // "zones", "words", "system", "reporting", "keypads", or null (full rescan)

function _updateRescanProgress(step, total, msg) {
  const pct = total > 0 ? Math.min(100, Math.round((step / total) * 100)) : 0;
  $("#rescan-progress-bar").css("width", pct + "%");
  $("#rescan-progress-step").text(`Step ${step} of ${total}`);
  if (msg) $("#rescan-progress-msg").text(msg);
}

function _endRescan() {
  if (!_rescanMode) return;
  _rescanMode = false;
  _rescanSection = null;
  $("#rescan-progress-panel").addClass("hidden");
  $(".tab-bar .tab-btn").removeClass("disabled").prop("disabled", false);
  $(".tab-panel").removeClass("tab-disabled");
  $(".btn-section-rescan").prop("disabled", false);
}

function _showRescanError(errorMsg) {
  $("#rescan-error-msg").text(errorMsg);
  $("#rescan-error-panel").removeClass("hidden");
  $("#rescan-debug-panel").addClass("hidden");
  $("#rescan-debug-log").text("");
  $("#btn-rescan-debug-toggle").text("Show debug log");
  // Dismiss — just hides the error panel
  $("#btn-rescan-error-dismiss").off("click").on("click", function () {
    $("#rescan-error-panel").addClass("hidden");
  });
  // Toggle debug log visibility and lazy-load entries
  $("#btn-rescan-debug-toggle").off("click").on("click", async function () {
    const $panel = $("#rescan-debug-panel");
    if ($panel.is(":visible")) {
      $panel.addClass("hidden");
      $(this).text("Show debug log");
      return;
    }
    $(this).text("Loading\u2026");
    try {
      const data = await api("GET", "/api/scan_log");
      const lines = (data.entries || []).map(e => {
        const ts = new Date(e.t * 1000).toISOString().substr(11, 12);
        const flag = e.level === "error" ? "\u274C" : e.level === "warn" ? "\u26A0\uFE0F" : "\u2713";
        let line = `[${ts}] ${flag} ${e.step}`;
        if (e.keys)    line += `\n  keys:    ${e.keys}`;
        if (e.display) line += `\n  display: ${JSON.stringify(e.display)}`;
        if (e.note)    line += `\n  note:    ${e.note}`;
        return line;
      }).join("\n\n");
      $("#rescan-debug-log").text(lines || "(no entries)");
    } catch (err) {
      $("#rescan-debug-log").text(`Failed to load: ${err.message}`);
    }
    $panel.removeClass("hidden");
    $(this).text("Hide debug log");
  });
}

function setConnState(state) {
  const $dot = $("#conn-dot");
  $dot.removeClass("connected disconnected connecting").addClass(state);
  const stateText = { connected: "Connected", disconnected: "Disconnected", connecting: "Connecting\u2026" }[state] || state;
  const modeText = (APP.connMode === "direct") ? "Direct (EVL)" : "Via HA";
  $("#conn-label").text(`${modeText} \u00b7 ${stateText}`);
}

/* ═══════════════════════════════════════════════════════════════════════
   Two-stage UI reveal
═══════════════════════════════════════════════════════════════════════ */

async function tryLoadCache() {
  try {
    const config = await api("GET", "/api/config");
    APP.config = config;
    revealSettingsUi(config);
  } catch (e) {
    // 404 = no cache yet; that's fine — cold-start stays visible
    if (!e.message.includes("No config cache")) {
      toast(`Could not load config cache: ${e.message}`, "err");
    }
  }
}

function revealSettingsUi(config) {
  // Hide cold-start overlay
  $("#cold-start-panel").addClass("hidden");
  // Show refresh button + last-scanned badge
  const scannedAt = config.scanned_at
    ? new Date(config.scanned_at * 1000).toLocaleString()
    : "";
  if (scannedAt) {
    $("#last-scanned").text(`Last scanned: ${scannedAt}`).removeClass("hidden");
  }
  $("#btn-refresh-config").removeClass("hidden").prop("disabled", false);
  // Show settings UI
  $("#settings-ui").removeClass("hidden");
  // Notify other modules that config is ready
  $(document).trigger("configLoaded", [config]);
}

/* ═══════════════════════════════════════════════════════════════════════
   Installer code modal
═══════════════════════════════════════════════════════════════════════ */

async function showCodeModal(dismissable) {
  // Show close button only when reopened manually (not initial forced prompt)
  $("#code-modal-close").toggleClass("hidden", !dismissable);
  // Only reset fields on first-time prompt; keep existing values when reopened
  if (!dismissable) {
    $("#user-code-row").removeClass("hidden");
    $("#user-code-auto").addClass("hidden");
    $("#user-code-input").val("");
    $("#code-input").val("");
  }
  $("#code-error").addClass("hidden");
  $("#code-modal-overlay").removeClass("hidden");
  setTimeout(() => $("#code-input").trigger("focus"), 80);
  // Try to auto-load user code from saved value or HA config (only if field is empty)
  if (!$("#user-code-input").val()) {
    try {
      const hint = await api("GET", "/api/user_code_hint");
      if (hint.code) {
        $("#user-code-input").val(hint.code);
        $("#user-code-auto").removeClass("hidden");
      }
    } catch { /* non-critical — field stays visible */ }
  }
}
function hideCodeModal() {
  $("#code-modal-overlay").addClass("hidden");
}

async function submitCode() {
  const code = $("#code-input").val().trim();
  const userCode = $("#user-code-input").val().trim();
  const remember = $("#remember-code").prop("checked");
  const $err = $("#code-error");

  if (!/^\d{4,6}$/.test(code)) {
    $err.text("Invalid installer code — digits only, minimum 4.").removeClass("hidden");
    return;
  }
  if (userCode && !/^\d{4,6}$/.test(userCode)) {
    $err.text("User code must be digits only, minimum 4.").removeClass("hidden");
    return;
  }
  $err.addClass("hidden");

  try {
    await api("POST", "/api/code", { code, user_code: userCode, remember });
    APP.codeSet = true;
    APP.userCode = userCode;
    hideCodeModal();
    toast("Installer code accepted", "ok");
  } catch (e) {
    $err.text(e.message).removeClass("hidden");
  }
}

/* ═══════════════════════════════════════════════════════════════════════
   Generic confirm modal (replaces browser confirm())
═══════════════════════════════════════════════════════════════════════ */

/**
 * Show a modal confirmation dialog.
 * @param {Object} opts
 * @param {string} opts.title   - Modal heading text
 * @param {string} opts.body    - HTML body content
 * @param {string} [opts.icon]  - Emoji icon (default "⚠️")
 * @param {string} [opts.okLabel]  - Confirm button text (default "Confirm")
 * @param {boolean} [opts.danger] - If true, confirm button is styled as danger
 * @returns {Promise<boolean>} Resolves true if confirmed, false if cancelled
 */
function showConfirmModal(opts) {
  return new Promise((resolve) => {
    const $overlay = $("#confirm-modal");
    $("#confirm-modal-icon").text(opts.icon || "⚠️");
    $("#confirm-modal-title").text(opts.title || "Confirm");
    $("#confirm-modal-body").html(opts.body || "");
    const $ok = $("#confirm-modal-ok");
    $ok.text(opts.okLabel || "Confirm");
    $ok.toggleClass("btn-danger", !!opts.danger)
       .toggleClass("btn-primary", !opts.danger);
    $overlay.removeClass("hidden");

    function cleanup(result) {
      $overlay.addClass("hidden");
      $ok.off("click.cfm");
      $("#confirm-modal-cancel").off("click.cfm");
      $overlay.off("click.cfm");
      resolve(result);
    }
    $ok.on("click.cfm", () => cleanup(true));
    $("#confirm-modal-cancel").on("click.cfm", () => cleanup(false));
    $overlay.on("click.cfm", function (e) {
      if (e.target === this) cleanup(false);
    });
  });
}
window.showConfirmModal = showConfirmModal;

/* ═══════════════════════════════════════════════════════════════════════
   Tab switching
═══════════════════════════════════════════════════════════════════════ */

function initTabs() {
  const $bar = $(".tab-bar");

  // Update scroll-end class to toggle fade hint
  function updateScrollHint() {
    const el = $bar[0];
    if (!el) return;
    const atEnd = el.scrollLeft + el.clientWidth >= el.scrollWidth - 4;
    $bar.toggleClass("scroll-end", atEnd);
  }
  $bar.on("scroll", updateScrollHint);
  $(window).on("resize", updateScrollHint);
  setTimeout(updateScrollHint, 100);

  $bar.on("click", ".tab-btn", function () {
    const tab = $(this).data("tab");
    $(".tab-btn").removeClass("active").attr("aria-selected", "false");
    $(this).addClass("active").attr("aria-selected", "true");
    $(".tab-panel").removeClass("active");
    $(`#tab-${tab}`).addClass("active");
    // Scroll active tab into view on narrow screens
    this.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
    $(document).trigger("tabChanged", [tab]);
  });
}

/* ═══════════════════════════════════════════════════════════════════════
   Scan trigger
═══════════════════════════════════════════════════════════════════════ */

async function startScan(requireConfirm = false) {
  if (requireConfirm) {
    $("#refresh-modal").removeClass("hidden");
    return;
  }
  if (!APP.codeSet) { showCodeModal(); return; }
  try {
    updateScanProgress(0, 33, "Starting scan…");
    $("#scan-badge").removeClass("hidden");
    $("#btn-read-config").prop("disabled", true);
    $("#btn-refresh-config").prop("disabled", true);
    await api("POST", "/api/scan");
    // Outcome arrives via WS scan_complete / scan_error
  } catch (e) {
    const wasRescan = _rescanMode;
    $("#scan-progress-panel").addClass("hidden");
    $("#scan-badge").addClass("hidden");
    $("#btn-read-config").prop("disabled", false);
    $("#btn-refresh-config").prop("disabled", false);
    _endRescan();
    if (wasRescan) {
      _showRescanError(e.message);
    } else {
      toast(`Scan error: ${e.message}`, "err", 5000);
      $("#cold-start-hint, #btn-read-config").removeClass("hidden");
    }
  }
}

/* ═══════════════════════════════════════════════════════════════════════
   Theme toggle (bright / dark)
═══════════════════════════════════════════════════════════════════════ */

function initThemeToggle() {
  const $btn = $("#btn-theme-toggle");
  // Sync button icon with current theme
  function syncIcon() {
    const isLight = document.documentElement.getAttribute("data-theme") === "light";
    $btn.text(isLight ? "☀️" : "🌙");
    $btn.attr("title", isLight ? "Switch to dark theme" : "Switch to light theme");
  }
  syncIcon();

  $btn.on("click", function () {
    const isLight = document.documentElement.getAttribute("data-theme") === "light";
    if (isLight) {
      document.documentElement.removeAttribute("data-theme");
      localStorage.setItem("theme", "dark");
    } else {
      document.documentElement.setAttribute("data-theme", "light");
      localStorage.removeItem("theme");
    }
    syncIcon();
  });
}

/* ═══════════════════════════════════════════════════════════════════════
   Init
═══════════════════════════════════════════════════════════════════════ */

$(async function () {
  APP.basePath = getBasePath();

  initTabs();
  initThemeToggle();

  // Installer code modal buttons
  $("#code-submit").on("click", submitCode);
  $("#code-input").on("keydown", (e) => { if (e.key === "Enter") submitCode(); });
  $("#btn-clear-code").on("click", () => {
    showCodeModal(true);
  });
  $("#code-modal-close").on("click", hideCodeModal);

  // Scan buttons
  $("#btn-read-config").on("click", () => {
    if (!localStorage.getItem("connHintShown")) {
      localStorage.setItem("connHintShown", "1");
      toast("Tip: connection mode (HA or direct to EVL) can be changed via the button in the top-right corner", "ok");
    }
    startScan(false);
  });
  $("#btn-refresh-config").on("click", () => startScan(true));

  // Per-tab section rescan buttons
  $(".btn-section-rescan").on("click", async function () {
    if (!APP.codeSet) { showCodeModal(); return; }
    const section = $(this).data("section");
    $(".btn-section-rescan").prop("disabled", true);
    try {
      await api("POST", "/api/scan_section", { section });
      // Outcome arrives via WS scan_complete / scan_error
    } catch (e) {
      const wasRescan = _rescanMode;
      _endRescan();
      if (wasRescan) {
        _showRescanError(e.message);
      } else {
        toast(`Scan error: ${e.message}`, "err", 5000);
      }
    }
  });

  // Refresh confirm modal buttons
  $("#refresh-confirm").on("click", () => {
    $("#refresh-modal").addClass("hidden");
    if (!APP.codeSet) { showCodeModal(); return; }
    startScan(false);
  });
  $("#refresh-cancel").on("click", () => $("#refresh-modal").addClass("hidden"));
  $("#refresh-close").on("click", () => $("#refresh-modal").addClass("hidden"));
  $("#refresh-modal").on("click", function (e) {
    if (e.target === this) $("#refresh-modal").addClass("hidden");
  });

  // Connect WebSocket (will call showCodeModal if code not set, via WS code_status msg)
  connectWs();

  // Try to load cached config immediately
  await tryLoadCache();
});
