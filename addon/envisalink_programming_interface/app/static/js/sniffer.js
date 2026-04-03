/**
 * sniffer.js — Bus Sniffer tab
 *
 * Records every keypad display change (WS "display" events) while capture
 * is active.  Useful for observing what a physical keypad sends to the
 * panel — start capture, operate the keypad, stop capture, then read the
 * timestamped sequence to understand the programming navigation flow.
 *
 * No backend changes needed: the WS already delivers every state change
 * the EVL pushes from the panel bus.
 */

"use strict";

$(function () {

  let _capturing = false;
  let _viewMode = "live";   // "live" or "scanlog"
  const _log = [];          // live capture entries
  const _scanLog = [];      // scan log entries (loaded from server)

  // ------------------------------------------------------------------
  // Hook into global WS display events
  // app.js triggers "snifferDisplay" on every WS "display" message.
  // ------------------------------------------------------------------

  $(document).on("snifferDisplay", (_evt, display) => {
    if (!_capturing) return;
    const now = new Date();
    const ts = now.toTimeString().slice(0, 8) + "." +
               String(now.getMilliseconds()).padStart(3, "0");

    // Split the 32-char display into two 16-char lines
    const padded = display.padEnd(32, " ");
    const line1  = padded.slice(0, 16).trimEnd();
    const line2  = padded.slice(16, 32).trimEnd();

    _log.push({ type: "disp", ts, line1, line2, raw: display });
    if (_viewMode === "live") {
      _appendDispRow(ts, line1, line2);
      _updateCount();
    }
  });

  // Keypress events — fired by app.js on WS "keypress_sent" messages from the server.
  // source: "keypad", "configure:zone", "configure:time", "eventlog", "recovery", etc.
  $(document).on("snifferKeypress", (_evt, keys, source) => {
    if (!_capturing) return;
    const now = new Date();
    const ts = now.toTimeString().slice(0, 8) + "." +
               String(now.getMilliseconds()).padStart(3, "0");
    _log.push({ type: "key", ts, keys, source: source || "" });
    if (_viewMode === "live") {
      _appendKeyRow(ts, keys, source);
      _updateCount();
    }
  });

  // ------------------------------------------------------------------
  // UI helpers
  // ------------------------------------------------------------------

  function _appendDispRow(ts, line1, line2) {
    const $row = $("<tr class='sniff-row-disp'></tr>").append(
      $("<td></td>").text(ts),
      $("<td class='sniff-type'></td>").text("\u2190 DSP"),
      $("<td class='sniff-cell'></td>").text(line1 || "\u00a0"),
      $("<td class='sniff-cell'></td>").text(line2 || "\u00a0")
    );
    $("#sniff-tbody").append($row);
    // Auto-scroll to bottom
    _scrollToBottom();
    $("#sniff-empty").addClass("hidden");
    $("#sniff-table").removeClass("hidden");
  }

  function _appendKeyRow(ts, keys, source) {
    const label = source && source !== "keypad" ? `\u2192 KEY [${source}]` : "\u2192 KEY";
    const $row = $("<tr class='sniff-row-key'></tr>").append(
      $("<td></td>").text(ts),
      $("<td class='sniff-type'></td>").text(label),
      $("<td class='sniff-cell' colspan='2'></td>").text(keys)
    );
    $("#sniff-tbody").append($row);
    _scrollToBottom();
    $("#sniff-empty").addClass("hidden");
    $("#sniff-table").removeClass("hidden");
  }

  function _scrollToBottom() {
    const tbody = document.getElementById("sniff-tbody");
    if (tbody.lastElementChild) {
      tbody.lastElementChild.scrollIntoView({ block: "nearest" });
    }
  }

  function _updateCount() {
    if (_viewMode === "scanlog") {
      $("#sniff-count").text(`${_scanLog.length} scan log entries`);
    } else {
      const n = _log.length;
      $("#sniff-count").text(n === 1 ? "1 entry" : `${n} entries`);
    }
  }

  function _setCapturing(active) {
    _capturing = active;
    const $btn = $("#btn-sniff-toggle");
    if (active) {
      $btn.text("Stop Capture").removeClass("btn-sniff-start").addClass("btn-sniff-stop");
    } else {
      $btn.text("Start Capture").removeClass("btn-sniff-stop").addClass("btn-sniff-start");
    }
  }

  /** Render all live _log entries into the table. */
  function _renderLiveLog() {
    $("#sniff-tbody").empty();
    _log.forEach(e => {
      if (e.type === "key") {
        _appendKeyRow(e.ts, e.keys, e.source);
      } else {
        _appendDispRow(e.ts, e.line1, e.line2);
      }
    });
    if (_log.length === 0) {
      $("#sniff-table").addClass("hidden");
      $("#sniff-empty").removeClass("hidden");
    }
    _updateCount();
  }

  /** Switch between "live" and "scanlog" view modes. */
  function _setViewMode(mode) {
    _viewMode = mode;
    if (mode === "scanlog") {
      // Show scan log view — hide live controls, show back button
      $("#btn-sniff-toggle, #btn-sniff-clear").addClass("hidden");
      $("#btn-sniff-scanlog").addClass("hidden");
      $("#btn-sniff-back").removeClass("hidden");
      // Render scan log entries
      $("#sniff-tbody").empty();
      _scanLog.forEach(e => {
        _appendScanRow(e.ts, e.level, e.step, e.keys, e.display, e.note);
      });
      if (_scanLog.length === 0) {
        $("#sniff-table").addClass("hidden");
        $("#sniff-empty").removeClass("hidden").text("No scan log entries.");
      }
      _updateCount();
    } else {
      // Back to live view — restore controls
      $("#btn-sniff-toggle, #btn-sniff-clear").removeClass("hidden");
      $("#btn-sniff-scanlog").removeClass("hidden");
      $("#btn-sniff-back").addClass("hidden");
      $("#sniff-empty").text("Start capture, then operate your physical keypad.");
      _renderLiveLog();
    }
  }

  // ------------------------------------------------------------------
  // Button handlers
  // ------------------------------------------------------------------

  $("#btn-sniff-toggle").on("click", function () {
    _setCapturing(!_capturing);
  });

  $("#btn-sniff-clear").on("click", function () {
    _log.length = 0;
    $("#sniff-tbody").empty();
    $("#sniff-table").addClass("hidden");
    $("#sniff-empty").removeClass("hidden");
    _updateCount();
  });

  /** Back to Live button — returns from scan log view to the live capture. */
  $("#btn-sniff-back").on("click", function () {
    _setViewMode("live");
  });

  function _copyToClipboard(text) {
    // navigator.clipboard requires a secure context (HTTPS/localhost).
    // Fall back to execCommand for plain-HTTP HA addon environments.
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text)
        .then(() => toast("Log copied to clipboard", "ok"))
        .catch(() => _execCommandCopy(text));
    } else {
      _execCommandCopy(text);
    }
  }

  function _execCommandCopy(text) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.cssText = "position:fixed;opacity:0;top:0;left:0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    try {
      document.execCommand("copy");
      toast("Log copied to clipboard", "ok");
    } catch (_) {
      toast("Clipboard unavailable", "err");
    }
    document.body.removeChild(ta);
  }

  $("#btn-sniff-copy").on("click", function () {
    const source = _viewMode === "scanlog" ? _scanLog : _log;
    if (source.length === 0) { toast("Nothing to copy", "err"); return; }
    const lines = ["Time         | Type  | Content"];
    lines.push("-".repeat(60));
    source.forEach(e => {
      if (e.type === "key") {
        const src = e.source && e.source !== "keypad" ? ` [${e.source}]` : "";
        lines.push(`${e.ts.padEnd(13)}| -> KEY${src.padEnd(20)} | ${e.keys}`);
      } else if (e.type === "scan") {
        lines.push(`${e.ts.padEnd(13)}| ${e.level.padEnd(5)} LOG | ${e.step}  |  ${e.content}`);
      } else {
        lines.push(`${e.ts.padEnd(13)}| <- DSP | ${(e.line1 || "").padEnd(16)} | ${e.line2 || ""}`);
      }
    });
    _copyToClipboard(lines.join("\n"));
  });

  // ------------------------------------------------------------------
  // Scan Log — load from /api/scan_log and display in this table
  // ------------------------------------------------------------------

  function _appendScanRow(ts, level, step, keys, display, note) {
    const levelIcon = level === "error" ? "\u2717" : level === "warn" ? "\u26a0" : "\u2713";
    const content = [keys && `keys:${keys}`, display, note].filter(Boolean).join("  |  ");
    const $row = $(`<tr class="sniff-row-scan sniff-scan-${level}"></tr>`).append(
      $("<td></td>").text(ts),
      $("<td class='sniff-type'></td>").text(`${levelIcon} LOG`),
      $("<td class='sniff-cell'></td>").text(step),
      $("<td class='sniff-cell'></td>").text(content)
    );
    $("#sniff-tbody").append($row);
    _scrollToBottom();
    $("#sniff-empty").addClass("hidden");
    $("#sniff-table").removeClass("hidden");
  }

  $("#btn-sniff-scanlog").on("click", async function () {
    let entries;
    try {
      const data = await api("GET", "/api/scan_log");
      entries = data.entries || [];
    } catch (e) {
      toast("Failed to load scan log: " + e.message, "err");
      return;
    }
    if (entries.length === 0) {
      toast("No scan log available \u2014 run Read Panel Config first", "warn");
      return;
    }
    // Store scan log entries and switch view (live capture continues in background)
    _scanLog.length = 0;
    entries.forEach(e => {
      const ts = new Date(e.t * 1000).toTimeString().slice(0, 8);
      const content = [e.keys && `keys:${e.keys}`, e.display, e.note].filter(Boolean).join("  |  ");
      _scanLog.push({ type: "scan", ts, level: e.level, step: e.step, keys: e.keys || "", display: e.display || "", note: e.note || "", content });
    });
    _setViewMode("scanlog");
    toast(`Loaded ${entries.length} scan log entries`, "ok");
  });

});
