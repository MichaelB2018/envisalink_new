/**
 * settings.js — System Settings tab
 *
 * Renders rows for: exit delay, entry delay 1, entry delay 2, and clock.
 * Each row shows the current value from the config cache and allows
 * inline editing + save. Saving POST /api/configure and updates the
 * scanned_at badge.
 */

"use strict";

/* ── Delay encode/decode helpers ──
 * The panel stores delay values 0–96 as literal seconds;
 * values 97–99 encode extended durations:
 *   97 → 120 s, 98 → 180 s, 99 → 240 s
 */
function _delayDecode(raw) {
  const map = { 97: 120, 98: 180, 99: 240 };
  return map[raw] !== undefined ? map[raw] : raw;
}
function _delayEncode(seconds) {
  const map = { 120: 97, 180: 98, 240: 99 };
  return map[seconds] !== undefined ? map[seconds] : seconds;
}
/**
 * Parse a scanned delay value (raw digit string from scrolling read,
 * or legacy integer from old cache).
 * Returns { p1: <seconds>, p2: <seconds> }.
 */
function _parseDelayDual(val) {
  const str = String(val != null ? val : "");
  if (str.length >= 4 && /^\d{4,}$/.test(str)) {
    return {
      p1: _delayDecode(parseInt(str.substring(0, 2), 10)),
      p2: _delayDecode(parseInt(str.substring(2, 4), 10)),
    };
  }
  // Legacy: single integer (seconds) — show as Part 1 only
  const n = parseInt(str, 10);
  return { p1: isNaN(n) ? 0 : n, p2: 0 };
}

$(function () {

  const SETTINGS = [
    {
      id:    "fire_timeout",
      field: "fire_timeout",
      label: "Fire Alarm Timeout",
      hint:  "Whether the fire sounder stops at timeout (*32). UL fire installations must use 1.",
      type:  "select",
      options: [
        { value: 0, label: "0 — Stops at timeout" },
        { value: 1, label: "1 — No timeout (UL)" },
      ],
    },
    {
      id:    "bell_timeout",
      field: "bell_timeout",
      label: "Bell (Alarm) Timeout",
      hint:  "How long the alarm sounder runs before auto-silencing (*33).",
      type:  "select",
      options: [
        { value: 0, label: "0 — None (no timeout)" },
        { value: 1, label: "1 — 4 minutes" },
        { value: 2, label: "2 — 8 minutes" },
        { value: 3, label: "3 — 12 minutes" },
        { value: 4, label: "4 — 16 minutes" },
      ],
    },
    {
      id:    "exit_delay",
      field: "exit_delay",
      label: "Exit Delay (Part 1 / Part 2)",
      hint:  "Seconds before alarm arms after arming (field *34). 0–96 s direct; or 120 s. Zones type 01/02 use this delay.",
      type:  "delay_dual",
      min: 0, max: 120, step: 1,
      specials: [120],   // only 97=120 s; 98/99 do NOT apply to *34
    },
    {
      id:    "entry_delay_1",
      field: "entry_delay_1",
      label: "Entry Delay 1 (Part 1 / Part 2)",
      hint:  "Time to disarm after Entry/Exit #1 zone trips (field *35). 0–96 s direct; or 120, 180, 240 s. Used by Zone Type 01.",
      type:  "delay_dual",
      min: 0, max: 240, step: 1,
      specials: [120, 180, 240],
    },
    {
      id:    "entry_delay_2",
      field: "entry_delay_2",
      label: "Entry Delay 2 (Part 1 / Part 2)",
      hint:  "Time to disarm after Entry/Exit #2 zone trips (field *36). 0–96 s direct; or 120, 180, 240 s. Used by Zone Type 02.",
      type:  "delay_dual",
      min: 0, max: 240, step: 1,
      specials: [120, 180, 240],
    },
    {
      id:    "panel_time",
      field: "time",
      label: "Panel Clock",
      hint:  "Read the panel's current time, or sync it to your browser clock.",
      type:  "panel_time",
    },
  ];

  function buildPanelTimeStatus(pt) {
    const skew   = pt.skew_seconds != null ? Math.abs(pt.skew_seconds) : null;
    const isAhead = pt.skew_seconds != null && pt.skew_seconds < 0;
    const warn   = skew != null && skew > 180;
    const $el    = $('<div class="panel-time-info"></div>');
    if (pt.iso) {
      const d       = new Date(pt.iso);
      const timeStr = d.toLocaleString(undefined, {
        month: "short", day: "numeric", year: "numeric",
        hour: "2-digit", minute: "2-digit",
      });
      $el.append($('<span class="panel-time-val"></span>').text("Panel: " + timeStr));
    }
    if (skew != null) {
      const mins  = Math.floor(skew / 60);
      const secs  = Math.round(skew % 60);
      const dir   = isAhead ? "ahead" : "behind";
      const label = mins > 0 ? `${mins}m ${secs}s ${dir}` : `${secs}s ${dir}`;
      $el.append(
        $('<span></span>').addClass(warn ? "time-skew-warn" : "time-skew-ok").text(
          warn ? `⚠ ${label} — sync recommended` : `✓ ${label}`
        )
      );
    } else if (pt.display) {
      $el.append($('<span class="time-parse-err"></span>').text("Could not parse: " + pt.display.trim()));
    }
    return $el;
  }

  // Track the panel-time expiry timer so we can cancel/restart it
  let _panelTimeExpiryTimer = null;

  function renderSettings(config) {
    const delays = (config && config.delays) || {};
    const $list = $("#system-settings-list").empty();

    SETTINGS.forEach(s => {
      const currentVal = delays[s.field] != null ? delays[s.field] : "";

      // --- Panel Clock: special two-button row with expiry ---
      if (s.type === "panel_time") {
        const $timeDisplay = $('<div class="panel-time-info"></div>');
        const $readBtn  = $('<button class="btn-save">Read Clock</button>');
        const $syncBtn  = $('<button class="btn-save" style="margin-left:0.4rem">Sync Now</button>');
        const $status   = $('<span class="save-status"></span>');

        const $row = $('<div class="setting-row"></div>').append(
          $('<div class="setting-label"></div>').append(
            $('<span></span>').text(s.label),
            $('<small></small>').text(s.hint)
          ),
          $('<div class="setting-control"></div>').append(
            $timeDisplay, $readBtn, $syncBtn, $status
          )
        );
        $list.append($row);

        function showPanelTime(pt) {
          $timeDisplay.empty();
          if (!pt) return;
          const $el = buildPanelTimeStatus(pt);
          $timeDisplay.append($el);
          // Expire after 60 s so stale times don't linger
          if (_panelTimeExpiryTimer) clearTimeout(_panelTimeExpiryTimer);
          _panelTimeExpiryTimer = setTimeout(() => {
            $timeDisplay.empty().append(
              $('<span class="time-stale"></span>').text("(stale — click Read Clock to refresh)")
            );
          }, 60_000);
        }

        // Show cached time from last scan only if it's < 60 s old
        if (config && config.panel_time && config.panel_time.scan_epoch) {
          const age = (Date.now() / 1000) - config.panel_time.scan_epoch;
          if (age < 60) {
            showPanelTime(config.panel_time);
          } else {
            $timeDisplay.append(
              $('<span class="time-stale"></span>').text("(click Read Clock to fetch panel time)")
            );
          }
        }

        $readBtn.on("click", async function () {
          if (!APP.userCode) { toast("Set user/master code first", "err"); return; }
          $readBtn.prop("disabled", true).addClass("saving");
          $status.text("…").removeClass("ok err");
          lockUI("Reading panel clock — waiting for panel to return…");
          try {
            const pt = await api("POST", "/api/panel_time");
            showPanelTime(pt);
            $status.text("✓").addClass("ok").removeClass("err");
          } catch (e) {
            $status.text("✗").addClass("err").removeClass("ok");
            toast(`Read failed: ${e.message}`, "err");
          } finally {
            unlockUI();
            $readBtn.prop("disabled", false).removeClass("saving");
            setTimeout(() => $status.text(""), 3000);
          }
        });

        $syncBtn.on("click", async function () {
          $syncBtn.prop("disabled", true).addClass("saving");
          $status.text("…").removeClass("ok err");
          lockUI("Syncing panel clock…");
          try {
            await api("POST", "/api/configure", { field: "time", value: null });
            $status.text("✓").addClass("ok").removeClass("err");
            toast("Panel clock synced", "ok");
          } catch (e) {
            $status.text("✗").addClass("err").removeClass("ok");
            toast(`Sync failed: ${e.message}`, "err");
          } finally {
            unlockUI();
            $syncBtn.prop("disabled", false).removeClass("saving");
            setTimeout(() => $status.text(""), 3000);
          }
        });
        return; // done with this row
      }

      // --- Delay dual-partition row (exit delay, entry delays) ---
      if (s.type === "delay_dual") {
        const parsed = _parseDelayDual(currentVal);
        const $inp1 = $(`<input type="number" id="setting-${s.id}-p1" class="setting-input"
                          min="${s.min}" max="${s.max}" step="${s.step}"
                          value="${parsed.p1}" style="width:5.5rem">`);
        const $inp2 = $(`<input type="number" id="setting-${s.id}-p2" class="setting-input"
                          min="${s.min}" max="${s.max}" step="${s.step}"
                          value="${parsed.p2}" style="width:5.5rem">`);
        const $status  = $('<span class="save-status"></span>');
        const $saveBtn = $(`<button class="btn-save" data-field="${s.field}">Save</button>`);

        const $row = $('<div class="setting-row"></div>').append(
          $('<div class="setting-label"></div>').append(
            $('<span></span>').text(s.label),
            $('<small></small>').text(s.hint)
          ),
          $('<div class="setting-control"></div>').append(
            $('<span class="delay-part-label">P1:</span>'), $inp1,
            $('<span class="delay-part-label">P2:</span>'), $inp2,
            $saveBtn, $status
          )
        );
        $list.append($row);

        $saveBtn.on("click", async function () {
          const sec1 = parseInt($inp1.val(), 10);
          const sec2 = parseInt($inp2.val(), 10);
          if (isNaN(sec1) || isNaN(sec2)) { toast("Enter values for both partitions", "err"); return; }
          const specials = s.specials || [];
          for (const sec of [sec1, sec2]) {
            if (sec > 96 && !specials.includes(sec)) {
              toast(`Valid values: 0–96 s, or ${specials.join(", ")} s`, "err");
              return;
            }
          }
          const raw1 = _delayEncode(sec1);
          const raw2 = _delayEncode(sec2);
          const rawStr = String(raw1).padStart(2, "0") + String(raw2).padStart(2, "0");

          $saveBtn.prop("disabled", true).addClass("saving");
          $status.text("…").removeClass("ok err");
          lockUI(`Saving ${s.label}…`);
          try {
            await api("POST", "/api/configure", { field: s.field, value: rawStr });
            $status.text("✓").addClass("ok").removeClass("err");
            toast(`${s.label} saved`, "ok");
            if (APP.config && APP.config.delays) {
              APP.config.delays[s.field] = rawStr;
            }
          } catch (e) {
            $status.text("✗").addClass("err").removeClass("ok");
            toast(`Save failed: ${e.message}`, "err");
          } finally {
            unlockUI();
            $saveBtn.prop("disabled", false).removeClass("saving");
            setTimeout(() => $status.text(""), 3000);
          }
        });
        return;
      }

      // --- Standard settings (select, text, number, button) ---
      let $control;
      if (s.type === "button") {
        $control = $(`<button class="btn-save" data-field="${s.field}">${s.btnLabel}</button>`);
      } else if (s.type === "select") {
        $control = $(`<select id="setting-${s.id}" class="setting-input" data-original="${currentVal}">`);
        (s.options || []).forEach(opt => {
          const sel = String(opt.value) === String(currentVal) ? " selected" : "";
          $control.append(`<option value="${opt.value}"${sel}>${opt.label}</option>`);
        });
      } else if (s.type === "text") {
        $control = $(
          `<input type="text" id="setting-${s.id}" class="setting-input"
                  placeholder="${s.placeholder || ""}"
                  value="${currentVal}" data-original="${currentVal}">`
        );
      } else {
        $control = $(
          `<input type="number" id="setting-${s.id}" class="setting-input"
                  min="${s.min}" max="${s.max}" step="${s.step}"
                  value="${currentVal}" data-original="${currentVal}">`
        );
      }

      const $status = $('<span class="save-status"></span>');
      const $saveBtn = s.type === "button"
        ? $control
        : $(`<button class="btn-save" data-field="${s.field}">Save</button>`);

      const $row = $('<div class="setting-row"></div>').append(
        $('<div class="setting-label"></div>').append(
          $('<span></span>').text(s.label),
          $('<small></small>').text(s.hint)
        ),
        $('<div class="setting-control"></div>').append(
          s.type !== "button" ? $control : [],
          $saveBtn,
          $status
        )
      );

      $list.append($row);

      $saveBtn.on("click", async function () {
        const field = $(this).data("field");
        let value;
        if (s.type === "select") {
          value = parseInt($control.val(), 10);
          if (isNaN(value)) { toast("Invalid value", "err"); return; }
        } else {
          value = parseInt($control.val(), 10);
          if (isNaN(value)) { toast("Invalid value", "err"); return; }
          // Field-specific special values (from official programming guide):
          // *34 exit delay:   97=120s only (98/99 not valid)
          // *35/*36 entry delays: 97=120s, 98=180s, 99=240s
          const specials = s.specials || [];
          if (value > 96 && !specials.includes(value)) {
            const allowed = specials.join(", ");
            toast(`Valid values: 0–96 s, or ${allowed} s`, "err");
            return;
          }
        }

        $saveBtn.prop("disabled", true).addClass("saving");
        $status.text("…").removeClass("ok err");
        lockUI(`Saving ${s.label}…`);

        try {
          await api("POST", "/api/configure", { field, value: value });
          $status.text("✓").addClass("ok").removeClass("err");
          $control.data("original", value);
          toast(`${s.label} saved`, "ok");
        } catch (e) {
          $status.text("✗").addClass("err").removeClass("ok");
          toast(`Save failed: ${e.message}`, "err");
        } finally {
          unlockUI();
          $saveBtn.prop("disabled", false).removeClass("saving");
          setTimeout(() => $status.text(""), 3000);
        }
      });
    });
  }

  // Render when config is loaded
  $(document).on("configLoaded", (_evt, config) => renderSettings(config));

  // Re-render if we already have config when this script loads
  if (APP.config) renderSettings(APP.config);

  // -----------------------------------------------------------------------
  // Custom word section helpers
  // -----------------------------------------------------------------------

  function renderCustomWords(config) {
    const cw = (config && config.custom_words) || {};
    const $section = $("#custom-words-list").empty();

    const slots = [];
    for (let i = 1; i <= 12; i++) {
      const key = String(i).padStart(2, "0");
      const entry = cw[key] || {};
      slots.push({ word_num: i, key, content: entry.content || "" });
    }

    const customWordSlots = slots.filter(slot => slot.word_num <= 10);
    const partitionSlots = slots.filter(slot => slot.word_num >= 11);
    const PARTITION_LABELS = { 11: "Partition 1 Name", 12: "Partition 2 Name" };

    const buildInput = (slot, placeholder) => $('<input type="text" class="cw-input" maxlength="10">')
      .val(slot.content)
      .attr("data-original", slot.content)
      .attr("placeholder", placeholder);

    const buildActionCell = (slot, label, $input) => {
      const wn = String(slot.word_num).padStart(2, "0");
      const $status = $('<span class="save-status"></span>');
      const $saveBtn = $('<button class="btn-save cw-save-btn">Save</button>');
      const $delBtn = $('<button class="btn-save btn-ghost cw-del-btn" title="Clear field">✕</button>');

      $saveBtn.on("click", async function () {
        const text = $input.val().trim().toUpperCase();
        if (text && !/^[A-Z0-9 ]+$/.test(text)) {
          toast("Only A-Z, 0-9, and space are allowed", "err");
          return;
        }
        $saveBtn.prop("disabled", true).addClass("saving");
        $status.text("…").removeClass("ok err");
        lockUI(`Saving ${label}…`);
        try {
          await api("POST", "/api/configure", {
            field: "custom_word",
            word_num: slot.word_num,
            text: text,
          });
          $status.text("✓").addClass("ok").removeClass("err");
          $input.data("original", text).val(text);
          // Update local config cache
          if (APP.config) {
            const cwCache = APP.config.custom_words = APP.config.custom_words || {};
            cwCache[wn] = { word_num: slot.word_num, content: text, raw_display: "CUSTOM? " + wn + " " + text };
          }
          toast(`${label} saved`, "ok");
        } catch (e) {
          $status.text("✗").addClass("err").removeClass("ok");
          toast(`Save failed: ${e.message}`, "err");
        } finally {
          unlockUI();
          $saveBtn.prop("disabled", false).removeClass("saving");
          setTimeout(() => $status.text(""), 3000);
        }
      });

      $delBtn.on("click", async function () {
        if (!$input.val().trim() && !$input.attr("data-original")) return;
        $delBtn.prop("disabled", true);
        $status.text("…").removeClass("ok err");
        lockUI(`Clearing ${label}…`);
        try {
          await api("POST", "/api/configure", {
            field: "custom_word",
            word_num: slot.word_num,
            text: "",
          });
          $status.text("✓").addClass("ok").removeClass("err");
          $input.val("").attr("data-original", "");
          if (APP.config) {
            const cwCache = APP.config.custom_words = APP.config.custom_words || {};
            cwCache[wn] = { word_num: slot.word_num, content: "", raw_display: "" };
          }
          toast(`${label} cleared`, "ok");
        } catch (e) {
          $status.text("✗").addClass("err").removeClass("ok");
          toast(`Clear failed: ${e.message}`, "err");
        } finally {
          unlockUI();
          $delBtn.prop("disabled", false);
          setTimeout(() => $status.text(""), 3000);
        }
      });

      return $('<td class="cw-action-cell"></td>').append($delBtn, $saveBtn, $status);
    };

    const $wordsSection = $('<div class="cw-subsection"></div>');
    $wordsSection.append(
      '<div class="cw-subheader"><h4>Custom Words</h4><p>Words 1-10 are user-defined vocabulary used in zone descriptors as words 245-254.</p></div>'
    );

    const $wordsTable = $('<table class="cw-table"></table>');
    $wordsTable.append('<thead><tr><th>#</th><th>Word</th><th></th></tr></thead>');
    const $wordsBody = $('<tbody></tbody>');
    customWordSlots.forEach(slot => {
      const wn = String(slot.word_num).padStart(2, "0");
      const $input = buildInput(slot, "(empty)");
      const $tr = $('<tr></tr>').append(
        $('<td class="cw-num-cell"></td>').text(wn),
        $('<td class="cw-word-cell"></td>').append($input),
        buildActionCell(slot, `Custom word ${wn}`, $input)
      );
      $wordsBody.append($tr);
    });
    $wordsTable.append($wordsBody);
    $wordsSection.append($wordsTable);

    const $partitionSection = $('<div class="cw-subsection cw-partition-section"></div>');
    $partitionSection.append(
      '<div class="cw-subheader"><h4>Partition Names</h4><p>These labels are used for your panel partitions.</p></div>'
    );

    const $partitionTable = $('<table class="cw-table cw-partition-table"></table>');
    $partitionTable.append('<thead><tr><th>Name</th><th>Value</th><th></th></tr></thead>');
    const $partitionBody = $('<tbody></tbody>');
    partitionSlots.forEach(slot => {
      const label = PARTITION_LABELS[slot.word_num];
      const $input = buildInput(slot, label);
      const $tr = $('<tr class="cw-partition-row"></tr>').append(
        $('<td class="cw-partition-label-cell"></td>').text(label),
        $('<td class="cw-word-cell"></td>').append($input),
        buildActionCell(slot, label, $input)
      );
      $partitionBody.append($tr);
    });
    $partitionTable.append($partitionBody);
    $partitionSection.append($partitionTable);

    $section.append($wordsSection, $partitionSection);
  }

  $(document).on("configLoaded", (_evt, config) => renderCustomWords(config));
  if (APP.config) renderCustomWords(APP.config);
});
// ── Connection Settings ─────────────────────────────────────────────────────
// Rendered into #conn-settings-body inside the modal opened by #btn-conn-settings.
// The header button (#conn-label) doubles as the mode+state indicator.
// ---------------------------------------------------------------------------
$(function () {
  "use strict";

  // ── Modal open / close ─────────────────────────────────────────────────────
  $("#btn-conn-settings").on("click", () => {
    $("#conn-settings-modal").removeClass("hidden");
  });
  $("#btn-conn-modal-close").on("click", () => {
    $("#conn-settings-modal").addClass("hidden");
  });
  $("#conn-settings-modal").on("click", function (e) {
    // Close on backdrop click
    if (e.target === this) $(this).addClass("hidden");
  });

  async function loadConnectionSettings() {
    let cfg = {};
    try {
      cfg = await api("GET", "/api/connection");
    } catch (e) {
      cfg = { mode: "ha", evl_host: "", evl_port: 4025, evl_password_set: false };
    }
    renderConnectionSettings(cfg);
  }

  function buildTextField(id, label, hint, value, placeholder) {
    return $('<div class="setting-row"></div>').append(
      $('<div class="setting-label"></div>').append(
        $('<span></span>').text(label),
        $('<div class="setting-hint"></div>').text(hint)
      ),
      $('<div class="setting-control"></div>').append(
        $('<input type="text" class="setting-input">').attr({ id, placeholder: placeholder || "" }).val(value)
      )
    );
  }

  function renderConnectionSettings(cfg) {
    const isDirect = cfg.mode === "direct";
    const $body = $("#conn-settings-body").empty();

    // ── Mode radio buttons ──────────────────────────────────────────────────
    const $modeRow = $('<div class="setting-row"></div>').append(
      $('<div class="setting-label"><span>Connection Mode</span></div>'),
      $('<div class="setting-control conn-radios"></div>').append(
        $('<label class="conn-radio-label"></label>').append(
          $('<input type="radio" name="conn-mode" value="ha">').prop("checked", !isDirect),
          " Via HA custom component"
        ),
        $('<label class="conn-radio-label"></label>').append(
          $('<input type="radio" name="conn-mode" value="direct">').prop("checked", isDirect),
          " Direct to EVL panel"
        )
      )
    );
    $body.append($modeRow);

    // ── HA-mode info panel (shown/hidden based on radio) ─────────────────────
    const $haFields = $('<div id="conn-ha-fields"></div>').toggle(!isDirect);
    const ks = cfg.keypad_sensor || 'sensor.envisalink_new_keypad_partition_1';
    const pe = cfg.partition_entity || 'alarm_control_panel.envisalink_new_partition_1';
    $haFields.append($(
      '<div class="conn-ha-info">' +
      'The addon communicates with the EVL <em>through</em> the ' +
      '<code>envisalink_new</code> HA integration (no direct TCP connection). ' +
      'Two HA entities are used:' +
      '<ul>' +
      '<li><strong>Keypad sensor</strong> (<code>' + ks + '</code>) — ' +
      'display updates via HA WebSocket.</li>' +
      '<li><strong>Alarm control panel</strong> (<code>' + pe + '</code>) — ' +
      'keypresses via <code>envisalink_new/alarm_keypress</code> service.</li>' +
      '</ul>' +
      'Entity IDs are auto-discovered and saved on first boot.' +
      '</div>'
    ));
    $body.append($haFields);

    // ── Direct-mode fields (shown/hidden based on radio) ─────────────────────
    const $directFields = $('<div id="conn-direct-fields"></div>').toggle(isDirect);

    $directFields.append(
      buildTextField("conn-evl-host", "EVL IP Address",
        "Hostname or IP address of the EyezOn Envisalink device on your LAN.",
        cfg.evl_host || ""),
      buildTextField("conn-evl-port", "EVL Port",
        "TPI TCP port — almost always 4025.",
        cfg.evl_port || 4025),
      buildTextField("conn-evl-password", "EVL Password",
        "TPI password set in the EyezOn portal (default: user). Leave blank to keep the saved value.",
        "", cfg.evl_password_set ? "(saved)" : "password")
    );

    $directFields.append($(
      '<div class="conn-direct-warn">' +
      '<strong>Important:</strong> The EVL only allows one TCP connection at a time. ' +
      'If you enable direct mode you <em>must</em> disable the ' +
      '<code>envisalink_new</code> integration in Home Assistant first, ' +
      'otherwise both will compete for the same connection and both will fail.' +
      '</div>'
    ));

    $body.append($directFields);

    // ── Save button ─────────────────────────────────────────────────────────
    const $saveBtn = $('<button class="btn-save" id="conn-save-btn">Save &amp; Reconnect</button>');
    const $saveStatus = $('<span class="save-status" id="conn-save-status"></span>');
    $body.append(
      $('<div class="setting-row"></div>').append(
        $('<div class="setting-label"></div>'),
        $('<div class="setting-control"></div>').append($saveBtn, $saveStatus)
      )
    );

    // Sync the status-bar label to the loaded mode immediately
    updateConnLabel(cfg.mode, cfg.mode === "direct" ? (cfg.connected === true) : null);

    // ── Show/hide direct fields when mode changes ──────────────────────────
    $body.on("change", 'input[name="conn-mode"]', function () {
      const direct = $(this).val() === "direct";
      $directFields.toggle(direct);
      $haFields.toggle(!direct);
    });

    // ── Save handler ────────────────────────────────────────────────────────
    $saveBtn.on("click", async function () {
      $saveBtn.prop("disabled", true).addClass("saving");
      $saveStatus.text("").removeClass("ok err");
      const mode = $('input[name="conn-mode"]:checked').val();
      const payload = {
        mode,
        evl_host: $("#conn-evl-host").val().trim(),
        evl_port: parseInt($("#conn-evl-port").val(), 10) || 4025,
        evl_password: $("#conn-evl-password").val(),
      };
      try {
        await api("POST", "/api/connection", payload);
        $saveStatus.text("✓ Saved").addClass("ok");
        updateConnLabel(mode, mode === "direct" ? false : null);  // show connecting state
      } catch (e) {
        $saveStatus.text("✗ " + e.message).addClass("err");
      } finally {
        $saveBtn.prop("disabled", false).removeClass("saving");
        setTimeout(() => $saveStatus.text("").removeClass("ok err"), 4000);
      }
    });
  }

  function updateConnLabel(mode, evlConnected) {
    // evlConnected: true = connected, false = connecting, null = use current HA state
    APP.connMode = mode || "ha";
    const modeText = mode === "direct" ? "Direct (EVL)" : "Via HA";
    let stateText;
    if (mode === "direct") {
      stateText = evlConnected === true ? "Connected" : "Connecting…";
    } else {
      // HA mode: re-use current dot state to infer label
      const dot = $("#conn-dot");
      stateText = dot.hasClass("connected") ? "Connected"
                : dot.hasClass("connecting") ? "Connecting…"
                : "Disconnected";
    }
    $("#conn-label").text(`${modeText} · ${stateText}`);
    $("#btn-conn-settings").attr("title", `${modeText} · ${stateText} — click to change settings`);
  }

  function updateModeBadge(mode) {
    updateConnLabel(mode, mode === "direct" ? false : null);
  }

  // Update the label when EvlClient reports a connection state change
  $(document).on("evlStatusUpdate", (_evt, connected) => {
    updateConnLabel("direct", connected);
  });

  loadConnectionSettings();
});