/**
 * keypads.js — Keypad Configuration tab
 *
 * Reads keypad settings (*190–*196) from the config cache and renders
 * one row per keypad (2–8, addresses 17–23).  Each row has:
 *   - Partition/Enable dropdown (0=Disabled, 1=Part 1, 2=Part 2, 3=Common)
 *   - Sound Option dropdown (0=All sounds, 1–3 suppression levels)
 *   - Save button → POST /api/configure { field:"keypad", ... }
 *
 * Keypad 1 (address 16) is factory-set and cannot be changed.
 */

"use strict";

$(function () {

  const PARTITION_OPTIONS = [
    { value: 0, label: "0 — Disabled" },
    { value: 1, label: "1 — Partition 1" },
    { value: 2, label: "2 — Partition 2" },
    { value: 3, label: "3 — Common" },
  ];

  const SOUND_OPTIONS = [
    { value: 0, label: "0 — All sounds enabled" },
    { value: 1, label: "1 — Suppress arm/disarm & entry/exit" },
    { value: 2, label: "2 — Suppress chime only" },
    { value: 3, label: "3 — Suppress all sounds" },
  ];

  function buildSelect(opts, selectedVal, cls) {
    const $sel = $(`<select class="${cls}"></select>`);
    opts.forEach(o => {
      const $opt = $(`<option></option>`).val(o.value).text(o.label);
      if (o.value === selectedVal) $opt.prop("selected", true);
      $sel.append($opt);
    });
    return $sel;
  }

  function renderKeypads(config) {
    const keypads = (config && config.keypads) || {};
    const $list = $("#keypad-settings-list").empty();

    for (let kn = 2; kn <= 8; kn++) {
      const key = String(kn);
      const kp = keypads[key] || {};
      const addr = 15 + kn;
      const pe  = parseInt(kp.partition_enable || "0", 10);
      const snd = parseInt(kp.sound || "0", 10);

      const $peSel  = buildSelect(PARTITION_OPTIONS, pe, "kp-partition");
      const $sndSel = buildSelect(SOUND_OPTIONS, snd, "kp-sound");
      const $saveBtn = $('<button class="btn-save">Save</button>');
      const $status  = $('<span class="save-status"></span>');

      const $row = $('<div class="setting-row"></div>').append(
        $('<div class="setting-label"></div>').append(
          $('<span></span>').text(`Keypad ${kn}  (addr ${addr}, *${188 + kn})`),
          $('<small></small>').text(
            kn === 2
              ? "Keypad 1 (addr 16) is factory-set and cannot be changed."
              : `Device address ${addr} on the keypad bus.`
          )
        ),
        $('<div class="setting-control"></div>').append(
          $peSel, $sndSel, $saveBtn, $status
        )
      );
      $list.append($row);

      $saveBtn.on("click", async function () {
        const newPe  = parseInt($peSel.val(), 10);
        const newSnd = parseInt($sndSel.val(), 10);
        $saveBtn.prop("disabled", true).addClass("saving");
        $status.text("…").removeClass("ok err");
        lockUI("Saving keypad " + kn + " — waiting for panel…");
        try {
          await api("POST", "/api/configure", {
            field: "keypad",
            keypad_num: kn,
            partition_enable: newPe,
            sound: newSnd,
          });
          $status.text("✓").addClass("ok").removeClass("err");
          toast(`Keypad ${kn} saved`, "ok");
        } catch (e) {
          $status.text("✗").addClass("err").removeClass("ok");
          toast("Save failed: " + (e.message || e), "err");
        } finally {
          unlockUI();
          $saveBtn.prop("disabled", false).removeClass("saving");
        }
      });
    }
  }

  // Re-render when the tab becomes visible (config may have been scanned)
  const $tab = $("#tab-keypads");
  const observer = new MutationObserver(() => {
    if ($tab.hasClass("active")) {
      api("GET", "/api/config").then(renderKeypads).catch(() => {});
    }
  });
  observer.observe($tab[0], { attributeFilter: ["class"] });

  // Also render on initial load if the tab is already active
  if ($tab.hasClass("active")) {
    api("GET", "/api/config").then(renderKeypads).catch(() => {});
  }

  // Expose for manual refresh
  window.renderKeypads = renderKeypads;
});
