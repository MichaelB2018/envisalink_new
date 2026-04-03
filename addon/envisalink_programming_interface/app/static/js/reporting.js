/**
 * reporting.js — Reporting / Dialer / Pager tab
 *
 * Renders fields *40–*49 (dialer) and *160–*172 (pager) from the
 * panel config cache and provides save/delete operations.
 */

"use strict";

/* ── Field definitions ──
 * Each entry: { key, field, label, hint, deletable, options }
 * - key:       matches the Python REPORTING_BY_KEY key
 * - field:     panel field number
 * - label:     display name
 * - hint:      description from the programming guide
 * - deletable: whether the field supports *NN* clear
 * - options:   if set, render as a <select>; otherwise text input
 */
const REPORTING_DEFS = [
  // ── Dialer ──
  { section: "Dialer Settings", key: "pabx", field: 40,
    label: "PABX / Call Waiting Disable (*40)", hint: "Up to 6 digits. Clear with ✱40✱.",
    deletable: true, maxlen: 6 },
  { section: "Dialer Settings", key: "primary_phone", field: 41,
    label: "Primary Phone No. (*41)", hint: "Up to 20 digits.",
    deletable: true, maxlen: 20 },
  { section: "Dialer Settings", key: "secondary_phone", field: 42,
    label: "Second Phone No. (*42)", hint: "Up to 20 digits.",
    deletable: true, maxlen: 20 },
  { section: "Dialer Settings", key: "part1_acct_pri", field: 43,
    label: "Part. 1 Primary Acct. No. (*43)", hint: "4 or 10 digits.",
    deletable: true, maxlen: 10 },
  { section: "Dialer Settings", key: "part1_acct_sec", field: 44,
    label: "Part. 1 Secondary Acct. No. (*44)", hint: "4 or 10 digits.",
    deletable: true, maxlen: 10 },
  { section: "Dialer Settings", key: "part2_acct_pri", field: 45,
    label: "Part. 2 Primary Acct. No. (*45)", hint: "4 or 10 digits.",
    deletable: true, maxlen: 10 },
  { section: "Dialer Settings", key: "part2_acct_sec", field: 46,
    label: "Part. 2 Secondary Acct. No. (*46)", hint: "4 or 10 digits.",
    deletable: true, maxlen: 10 },
  { section: "Dialer Settings", key: "phone_system", field: 47,
    label: "Phone System Select (*47)", hint: "0=Pulse, 1=Tone, 2=Pulse(WATS), 3=Tone(WATS).",
    deletable: false, maxlen: 1,
    options: [
      { value: "0", text: "0 — Pulse Dial" },
      { value: "1", text: "1 — Tone Dial" },
      { value: "2", text: "2 — Pulse Dial (WATS)" },
      { value: "3", text: "3 — Tone Dial (WATS)" },
    ]},
  { section: "Dialer Settings", key: "report_format", field: 48,
    label: "Report Format (*48)", hint: "Two digits: primary + secondary format.",
    deletable: false, maxlen: 2 },
  { section: "Dialer Settings", key: "split_dual", field: 49,
    label: "Split/Dual Reporting (*49)", hint: "0=Standard only.",
    deletable: false, maxlen: 1,
    options: [
      { value: "0", text: "0 — Standard/backup only" },
      { value: "1", text: "1 — Alarms/Restore/Cancel | Others" },
      { value: "2", text: "2 — All except O/C, Test | O/C, Test" },
      { value: "3", text: "3 — Alarms/Restore/Cancel | All" },
      { value: "4", text: "4 — All except O/C, Test | All" },
      { value: "5", text: "5 — All | All" },
    ]},

  // ── Pager 1 ──
  { section: "Pager 1", key: "pager1_phone", field: 160,
    label: "Pager 1 Phone No. (*160)", hint: "Up to 20 digits.",
    deletable: true, maxlen: 20 },
  { section: "Pager 1", key: "pager1_chars", field: 161,
    label: "Pager 1 Characters (*161)", hint: "Up to 16 prefix characters.",
    deletable: true, maxlen: 16 },
  { section: "Pager 1", key: "pager1_report", field: 162,
    label: "Pager 1 Report Options (*162)", hint: "3 digits: Part1, Part2, Common. 0=none, 1=O/C, 4=alarms, 5=all, 12/13=zone list.",
    deletable: false, maxlen: 3 },

  // ── Pager 2 ──
  { section: "Pager 2", key: "pager2_phone", field: 163,
    label: "Pager 2 Phone No. (*163)", hint: "Up to 20 digits.",
    deletable: true, maxlen: 20 },
  { section: "Pager 2", key: "pager2_chars", field: 164,
    label: "Pager 2 Characters (*164)", hint: "Up to 16 prefix characters.",
    deletable: true, maxlen: 16 },
  { section: "Pager 2", key: "pager2_report", field: 165,
    label: "Pager 2 Report Options (*165)", hint: "3 digits: Part1, Part2, Common.",
    deletable: false, maxlen: 3 },

  // ── Pager 3 ──
  { section: "Pager 3", key: "pager3_phone", field: 166,
    label: "Pager 3 Phone No. (*166)", hint: "Up to 20 digits.",
    deletable: true, maxlen: 20 },
  { section: "Pager 3", key: "pager3_chars", field: 167,
    label: "Pager 3 Characters (*167)", hint: "Up to 16 prefix characters.",
    deletable: true, maxlen: 16 },
  { section: "Pager 3", key: "pager3_report", field: 168,
    label: "Pager 3 Report Options (*168)", hint: "3 digits: Part1, Part2, Common.",
    deletable: false, maxlen: 3 },

  // ── Pager 4 ──
  { section: "Pager 4", key: "pager4_phone", field: 169,
    label: "Pager 4 Phone No. (*169)", hint: "Up to 20 digits.",
    deletable: true, maxlen: 20 },
  { section: "Pager 4", key: "pager4_chars", field: 170,
    label: "Pager 4 Characters (*170)", hint: "Up to 16 prefix characters.",
    deletable: true, maxlen: 16 },
  { section: "Pager 4", key: "pager4_report", field: 171,
    label: "Pager 4 Report Options (*171)", hint: "3 digits: Part1, Part2, Common.",
    deletable: false, maxlen: 3 },

  // ── Pager Delay ──
  { section: "Pager Options", key: "pager_delay", field: 172,
    label: "Pager Delay For Alarms (*172)", hint: "Applies to all pagers.",
    deletable: false, maxlen: 1,
    options: [
      { value: "0", text: "0 — None" },
      { value: "1", text: "1 — 1 minute" },
      { value: "2", text: "2 — 2 minutes" },
      { value: "3", text: "3 — 3 minutes" },
    ]},
];


function renderReporting(config) {
  const $list = $("#reporting-list").empty();
  const reporting = (config && config.reporting) || {};

  let currentSection = "";

  REPORTING_DEFS.forEach(def => {
    // Section header
    if (def.section !== currentSection) {
      currentSection = def.section;
      $list.append(`<div class="rpt-section-header">${currentSection}</div>`);
    }

    const cached = reporting[def.key] || {};
    const currentVal = cached.value || "";

    const $row = $(`<div class="setting-row rpt-row" data-key="${def.key}"></div>`);

    // Label column
    const $label = $('<div class="setting-label"></div>').append(
      $('<span></span>').text(def.label),
      $('<small></small>').text(def.hint)
    );

    // Control column
    const $ctrl = $('<div class="setting-control rpt-control"></div>');
    let $input;

    if (def.options) {
      $input = $('<select class="rpt-input"></select>');
      const normVal = currentVal.trim().replace(/^0+(?=\d)/, "");
      def.options.forEach(opt => {
        const selected = (normVal === opt.value) ? " selected" : "";
        $input.append(`<option value="${opt.value}"${selected}>${opt.text}</option>`);
      });
    } else {
      $input = $(`<input type="text" class="rpt-input" value="${_escAttr(currentVal)}"
                   maxlength="${def.maxlen}" placeholder="(empty)">`);
    }
    $input.attr("data-key", def.key);

    const $saveBtn = $('<button class="btn-save rpt-save-btn">Save</button>');
    const $status = $('<span class="rpt-status"></span>');

    $ctrl.append($input, $saveBtn);

    if (def.deletable) {
      const $delBtn = $('<button class="btn-delete rpt-del-btn" title="Clear this field on the panel">Delete</button>');
      $ctrl.append($delBtn);
      $delBtn.on("click", async function () {
        const ok = await showConfirmModal({
          icon: "🗑️",
          title: "Clear Field?",
          body: "Clear <strong>*" + def.field + " (" + def.label + ")</strong> on the panel?<br><br>" +
            "This will erase the stored value.",
          okLabel: "Clear",
          danger: true,
        });
        if (!ok) return;
        const $s = $status;
        $s.text("…").removeClass("ok err");
        lockUI(`Clearing *${def.field}…`);
        api("POST", "/api/configure", {
          field: "reporting_delete",
          reporting_key: def.key,
        }).then(() => {
          $s.text("✓ Cleared").addClass("ok");
          // Clear local input
          if ($input.is("select")) $input.val($input.find("option:first").val());
          else $input.val("");
          // Update APP.config cache
          if (APP.config && APP.config.reporting && APP.config.reporting[def.key]) {
            APP.config.reporting[def.key].value = "";
          }
          toast("Field cleared ✓", "ok");
        }).catch(err => {
          $s.text("✗ " + err.message).addClass("err");
          toast("Delete failed: " + err.message, "err");
        }).finally(() => {
          unlockUI();
        });
      });
    }

    $ctrl.append($status);

    $saveBtn.on("click", function () {
      const val = ($input.val() || "").trim();
      if (!val && !def.options) {
        $status.text("Enter a value").addClass("err");
        return;
      }
      $status.text("…").removeClass("ok err");
      lockUI(`Saving *${def.field}…`);
      api("POST", "/api/configure", {
        field: "reporting",
        reporting_key: def.key,
        value: val,
      }).then(() => {
        $status.text("✓ Saved").addClass("ok");
        // Update APP.config cache
        if (APP.config) {
          const rpt = APP.config.reporting = APP.config.reporting || {};
          rpt[def.key] = rpt[def.key] || { field: def.field, label: def.label };
          rpt[def.key].value = val;
        }
        toast("Saved ✓", "ok");
      }).catch(err => {
        $status.text("✗ " + err.message).addClass("err");
        toast("Save failed: " + err.message, "err");
      }).finally(() => {
        unlockUI();
      });
    });

    $row.append($label, $ctrl);
    $list.append($row);
  });
}

function _escAttr(s) {
  return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;")
                  .replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/* ── Lifecycle ── */
$(function () {
  $(document).on("configLoaded", function (_, config) {
    renderReporting(config);
  });
  // If config already loaded before this script runs
  if (APP.config) renderReporting(APP.config);
});
