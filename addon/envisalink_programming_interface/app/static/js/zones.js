/**
 * zones.js — Zone Configuration tab
 *
 * Supports zones 1-48 organised into three collapsible groups:
 *   Hardwired (1-8)   — includes HW type and Response Time columns
 *   Expansion (9-16)  — zone expander slots, no HW/RT
 *   Wireless  (17-48) — wireless zones, no HW/RT
 *
 * Configured zones show as full editable rows. Unused zones appear as
 * compact clickable badges — click one to promote it to a full row.
 */

"use strict";

$(function () {

  let ZONE_TYPES = [];
  let VOCAB      = [];

  const ZONE_GROUPS = [
    { id: "hardwired", label: "Hardwired Zones", start: 1,  end: 8,  hasHwRt: true },
    { id: "expansion", label: "Expansion Zones", start: 9,  end: 16, hasHwRt: false },
    { id: "wireless",  label: "Wireless Zones",  start: 17, end: 48, hasHwRt: false },
  ];

  const HW_TYPES = [
    { id: 0, name: "EOL", desc: "End of Line" },
    { id: 1, name: "NC",  desc: "Normally Closed" },
    { id: 2, name: "NO",  desc: "Normally Open" },
    { id: 3, name: "ZD",  desc: "Zone Doubling" },
    { id: 4, name: "DB",  desc: "Double Balanced" },
  ];

  const RESPONSE_TIMES = [
    { id: 0, name: "10 ms",  desc: "Fast loop response" },
    { id: 1, name: "350 ms", desc: "Standard response" },
    { id: 2, name: "700 ms", desc: "Slow response" },
    { id: 3, name: "1.2 s",  desc: "Very slow response" },
  ];

  let _zonesRenderToken = 0;

  async function ensureDropdownData() {
    // Always re-fetch vocab to pick up custom word changes (245-254).
    // Zone types are static and only need one fetch.
    const fetches = [api("GET", "/api/vocab")];
    if (!ZONE_TYPES.length) fetches.unshift(api("GET", "/api/zone_types"));
    const results = await Promise.all(fetches);
    if (!ZONE_TYPES.length) {
      ZONE_TYPES = results[0].zone_types;
      VOCAB      = results[1].vocab;
    } else {
      VOCAB      = results[0].vocab;
    }
  }

  function buildSelect(opts, selectedId, cls) {
    const $sel = $(`<select class="${cls}"></select>`);
    opts.forEach(o => {
      const $opt = $(`<option value="${o.id}">${o.name}</option>`);
      if (o.id === selectedId) $opt.prop("selected", true);
      if (o.desc) $opt.attr("title", o.desc);
      $sel.append($opt);
    });
    return $sel;
  }

  /**
   * Build a minimal 3-word name editor.
   * Returns a jQuery element with three searchable selects.
   * currentName: "FRONT DOOR" style string from scanned config.
   */
  function buildNameCell(currentName) {
    const words = (currentName || "").trim().split(/\s+/).filter(Boolean);
    const $wrap = $('<div class="name-editor"></div>');

    for (let i = 0; i < 3; i++) {
      const word = words[i] || "";
      const $sel = $(`<select class="word-sel" title="Word ${i+1}"></select>`);
      $sel.append('<option value="0">—</option>');
      VOCAB.forEach(v => {
        const $opt = $(`<option value="${v.id}">${v.word}</option>`);
        if (v.word === word) $opt.prop("selected", true);
        $sel.append($opt);
      });
      // If the scanned word isn't in standard vocab, add a read-only placeholder
      // so the user can see what's there rather than a blank "—".
      if (word && !VOCAB.find(v => v.word === word)) {
        const $custom = $(`<option value="0" selected>⚙ ${word} (custom)</option>`);
        $sel.prepend($custom);
      }
      $wrap.append($sel);
    }
    return $wrap;
  }

  /** Collect word IDs from the name editor cells. */
  function getWordIds($nameCell) {
    return $nameCell.find(".word-sel").map(function () {
      return parseInt($(this).val(), 10);
    }).get().filter(id => id !== 0);
  }

  /* ── Bypass helpers ── */

  /**
   * Update the bypass cell for a single zone row without rebuilding the table.
   *   bypassVal: true=bypassed, false=not bypassed, null/undefined=n/a
   */
  function updateBypassCell(zoneNum, bypassVal) {
    const $td = $(`.zone-group-table tr[data-zone="${zoneNum}"] .bypass-cell`);
    if (!$td.length) return;
    const isBypassed = bypassVal === true || bypassVal === 1 || bypassVal === "1";
    $td.empty();
    if (isBypassed) {
      $td.append(
        $('<button class="btn-pill btn-pill-clear" title="Clear all bypasses">Clear</button>')
          .on("click", () => bypassAction("clear"))
      );
    } else {
      $td.append(
        $('<button class="btn-pill btn-pill-bypass" title="Bypass this zone">Bypass</button>')
          .on("click", () => bypassAction("bypass", zoneNum))
      );
    }
  }

  /**
   * Send a bypass or clear-all-bypasses keypress sequence to the panel,
   * then refresh bypass states after a short delay.
   *   "bypass"  → {userCode}6{ZZ:02d}
   *   "clear"   → {userCode}1
   */
  async function bypassAction(action, zoneNum) {
    const code = APP.userCode;
    if (!code) {
      toast("Set your user code on the Settings tab first", "err");
      return;
    }
    let keys;
    if (action === "bypass") {
      keys = code + "6" + String(zoneNum).padStart(2, "0");
    } else {
      keys = code + "1";
    }
    lockUI(action === "bypass" ? `Bypassing zone ${zoneNum}…` : "Clearing all bypasses…");
    try {
      await api("POST", "/api/keypress", { keys });
      toast(action === "bypass" ? `Bypass zone ${zoneNum} sent` : "Clear bypass sent", "ok");
      // Apply immediate local feedback; background scan below will reconcile.
      if (action === "bypass") {
        updateBypassCell(zoneNum, true);
      } else {
        for (let z = 1; z <= 48; z++) updateBypassCell(z, false);
      }
      // Wait for HA to process the state change, then refresh bypass cells
      await new Promise(r => setTimeout(r, 4000));
      await scanBypassFromPanel({ silent: true, allowFallback: false });
    } catch (e) {
      toast(`Bypass command failed: ${e.message}`, "err");
    } finally {
      unlockUI();
    }
  }

  /**
   * Read the bypass list directly from the panel by scrolling the LCD display.
   * Calls POST /api/bypass_scan which sends '*' to scroll through bypassed zones.
   * Updates all bypass cells with the result.
   */
  let _bypassScanInFlight = false;
  async function scanBypassFromPanel(opts = {}) {
    const silent = !!opts.silent;
    const allowFallback = opts.allowFallback !== false;
    if (_bypassScanInFlight) return;   // prevent concurrent panel access
    _bypassScanInFlight = true;
    let bypassStates = {};
    try {
      const bs = await api("POST", "/api/bypass_scan");
      bypassStates = bs.zones || {};
      const bypassed = bs.bypassed || [];
      if (!silent && bypassed.length > 0) {
        toast(`Bypassed zones: ${bypassed.join(", ")}`, "ok");
      } else if (!silent) {
        toast("No zones bypassed", "ok");
      }
    } catch (e) {
      // If bypass scan fails (panel not in bypass mode), fall back to HA states
      if (!allowFallback) {
        _bypassScanInFlight = false;
        return;
      }
      try {
        const bs = await api("GET", "/api/zone_states");
        bypassStates = bs.zones || {};
      } catch (_) { _bypassScanInFlight = false; return; }
    }
    for (let z = 1; z <= 48; z++) {
      const val = bypassStates[String(z)];
      updateBypassCell(z, val !== undefined && val !== null ? val : false);
    }
    _bypassScanInFlight = false;
  }

  // "Read Bypass" button handler
  $("#btn-bypass-scan").on("click", async function () {
    const $btn = $(this);
    $btn.prop("disabled", true);
    lockUI("Reading bypassed zones from panel…");
    try {
      await scanBypassFromPanel();
    } finally {
      unlockUI();
      $btn.prop("disabled", false);
    }
  });

  /* ── Zone row builder ── */

  function buildZoneRow(z, zd, hasHwRt, bypassVal) {
    const $ztSel   = buildSelect(ZONE_TYPES, zd.zone_type ?? 0, "zone-type-sel");
    const $ztHint  = $('<div class="zt-hint"></div>');
    const _updateZtHint = (id) => {
      const entry = ZONE_TYPES.find(t => t.id === id);
      $ztHint.text(entry?.desc || "");
    };
    _updateZtHint(zd.zone_type ?? 0);
    $ztSel.on("change", function () { _updateZtHint(parseInt($(this).val(), 10)); });

    // Report Code: free text (2-digit, 00–15)
    const rcVal = zd.report_code != null ? String(zd.report_code).padStart(2, "0") : "01";
    const $rcInput = $(`<input type="text" class="rc-input" value="${rcVal}"
                         maxlength="2" title="00=disabled, 01–15=enabled (Contact ID: any non-zero)">`);

    const PART_OPTS = [
      { id: 0, name: "None" },
      { id: 1, name: "1" },
      { id: 2, name: "2" },
      { id: 3, name: "3" },
    ];
    const $partSel = buildSelect(PART_OPTS, zd.partition ?? 0, "part-sel");
    const $nameCel = buildNameCell(zd.name || "");
    const $status  = $('<span class="save-status"></span>');
    const $saveBtn = $('<button class="btn-save">Save</button>');

    // Bypass cell — default to showing Bypass button for any zone
    const $bypassCel = $('<td class="bypass-cell"></td>');
    if (bypassVal === true) {
      $bypassCel.append(
        $('<button class="btn-pill btn-pill-clear" title="Clear all bypasses">Clear</button>')
          .on("click", () => bypassAction("clear"))
      );
    } else {
      $bypassCel.append(
        $('<button class="btn-pill btn-pill-bypass" title="Bypass this zone">Bypass</button>')
          .on("click", () => bypassAction("bypass", z))
      );
    }

    // Build the row
    const $tr = $(`<tr data-zone="${z}"></tr>`);
    $tr.append(
      $('<td></td>').append(`<span class="zone-num-badge">${z}</span>`),
      $('<td></td>').append($nameCel),
      $('<td></td>').append($ztSel, $ztHint),
    );

    // HW + RT columns (hardwired zones only)
    let $hwSel, $rtSel;
    if (hasHwRt) {
      const storedHw = zd.hw_type != null ? zd.hw_type : Math.floor((zd.input_type || 1) / 10);
      $hwSel = buildSelect(HW_TYPES, storedHw, "hw-sel");
      if (z === 1) $hwSel.prop("disabled", true).val(0);  // Zone 1 always EOL
      const $hwHint = $('<div class="attr-hint"></div>');
      const _updateHwHint = (id) => {
        const entry = HW_TYPES.find(t => t.id === id);
        $hwHint.text(entry?.desc || "");
      };
      _updateHwHint(storedHw);
      $hwSel.on("change", function () { _updateHwHint(parseInt($(this).val(), 10)); });

      const storedRt = zd.response_time != null ? zd.response_time : (zd.input_type || 1) % 10;
      $rtSel = buildSelect(RESPONSE_TIMES, storedRt, "rt-sel");
      const $rtHint = $('<div class="attr-hint"></div>');
      const _updateRtHint = (id) => {
        const entry = RESPONSE_TIMES.find(t => t.id === id);
        $rtHint.text(entry?.desc || "");
      };
      _updateRtHint(storedRt);
      $rtSel.on("change", function () { _updateRtHint(parseInt($(this).val(), 10)); });

      $tr.append(
        $('<td></td>').append($hwSel, $hwHint),
        $('<td></td>').append($rtSel, $rtHint),
      );
    }

    $tr.append(
      $('<td></td>').append($rcInput),
      $('<td style="text-align:center"></td>').append($partSel),
      $bypassCel,
      $('<td></td>').append($saveBtn, $status)
    );

    // ── Save handler ──
    $saveBtn.on("click", async function () {
      const zone_num      = z;
      const zone_type     = parseInt($ztSel.val(), 10);
      const rc_raw        = ($rcInput.val() || "").trim();
      const report_code   = parseInt(rc_raw, 10);
      const partition     = parseInt($partSel.val(), 10);
      const word_ids      = getWordIds($nameCel);

      // Validate report code
      if (isNaN(report_code) || report_code < 0 || report_code > 15) {
        toast("Report code must be 00–15", "err");
        return;
      }

      $saveBtn.prop("disabled", true).addClass("saving");
      $status.text("…").removeClass("ok err");
      lockUI(`Saving zone ${zone_num} — programming panel…`);

      try {
        const payload = { field: "zone", zone_num, zone_type, partition, report_code };
        // Only include input_type for hardwired zones (1-8)
        if (hasHwRt) {
          payload.input_type = parseInt($hwSel.val(), 10) * 10 + parseInt($rtSel.val(), 10);
        }

        // Save zone type/attrs first, then name — sequential to avoid
        // interleaving programming-mode sequences on the panel.
        await api("POST", "/api/configure", payload);
        // Give the panel time to exit programming mode and settle
        // before starting the second programming sequence.
        await new Promise(r => setTimeout(r, 3000));
        await api("POST", "/api/configure", {
          field: "zone_name", zone_num, word_ids,
        });

        // Update local config so a page refresh shows saved data
        if (APP.config) {
          const zones = APP.config.zones = APP.config.zones || {};
          const zd = zones[String(zone_num)] = zones[String(zone_num)] || { zone: zone_num };
          zd.zone_type = zone_type;
          zd.partition = partition;
          zd.report_code = report_code;
          if (hasHwRt) {
            zd.input_type = payload.input_type;
            zd.hw_type = parseInt($hwSel.val(), 10);
            zd.response_time = parseInt($rtSel.val(), 10);
          }
          // Resolve word IDs to display name
          const names = word_ids.map(id => {
            const v = VOCAB.find(v => v.id === id);
            return v ? v.word : "";
          }).filter(Boolean);
          zd.name = names.join(" ");
        }

        $status.text("✓").addClass("ok").removeClass("err");
        toast(`Zone ${zone_num} saved`, "ok");
      } catch (e) {
        $status.text("✗").addClass("err").removeClass("ok");
        toast(`Zone ${zone_num} save failed: ${e.message}`, "err");
      } finally {
        unlockUI();
        $saveBtn.prop("disabled", false).removeClass("saving");
        setTimeout(() => $status.text(""), 3000);
      }
    });

    return $tr;
  }

  /** Re-sort table body rows by zone number after inserting a new one. */
  function sortTableRows($tbody) {
    const rows = $tbody.find("tr").detach().toArray();
    rows.sort((a, b) => parseInt($(a).data("zone")) - parseInt($(b).data("zone")));
    $tbody.append(rows);
  }

  /* ── Main render ── */

  async function renderZones(config) {
    await ensureDropdownData();
    const renderToken = ++_zonesRenderToken;

    const zones = (config && config.zones) || {};
    const $container = $("#zones-container").empty();
    const bypassStates = {};

    ZONE_GROUPS.forEach(group => {
      // Collect all zones in this group
      const allGroupZones = [];
      for (let z = group.start; z <= group.end; z++) {
        allGroupZones.push({ num: z, data: zones[String(z)] || { zone: z } });
      }

      const activeZones = allGroupZones.filter(gz => (gz.data.zone_type || 0) !== 0);
      const unusedZones = allGroupZones.filter(gz => (gz.data.zone_type || 0) === 0);
      const hasActive = activeZones.length > 0;

      // ── Group card ──
      const $card = $(`<div class="zone-group-card" data-group="${group.id}"></div>`);

      // Collapsible header
      const $header = $(`<div class="zone-group-header${hasActive ? '' : ' collapsed'}"></div>`);
      $header.append(
        '<span class="zone-group-toggle">▾</span>',
        `<span class="zone-group-label">${group.label}</span>`,
        `<span class="zone-group-range">${group.start}–${group.end}</span>`,
        `<span class="zone-group-badge">${activeZones.length} active</span>`
      );

      // Body
      const $body = $('<div class="zone-group-body"></div>');
      if (!hasActive) $body.hide();

      // Table
      const hwCols = group.hasHwRt ? '<th>HW</th><th>RT</th>' : '';
      const $table = $(`
        <table class="zones-table zone-group-table ${group.hasHwRt ? 'has-hw-rt' : 'no-hw-rt'}">
          <thead><tr>
            <th>Zone</th><th>Name</th><th>Type</th>${hwCols}<th>Report</th><th>Part.</th><th>Bypass</th><th></th>
          </tr></thead>
          <tbody></tbody>
        </table>
      `);
      const $tbody = $table.find("tbody");

      // Add rows for active (configured) zones
      activeZones.forEach(gz => {
        $tbody.append(buildZoneRow(gz.num, gz.data, group.hasHwRt, bypassStates[String(gz.num)]));
      });

      // Hide table if no active zones (shown when first badge is clicked)
      if (!hasActive) $table.hide();

      $body.append($table);

      // ── Unused zones section ──
      if (unusedZones.length > 0) {
        const $unused = $('<div class="zone-unused-section"></div>');
        $unused.append('<span class="zone-unused-label">Unused:</span>');

        unusedZones.forEach(gz => {
          const $badge = $(`<span class="zone-unused-badge" title="Click to configure zone ${gz.num}">${gz.num}</span>`);
          $badge.on("click", function () {
            // Show table if this is the first row
            if ($table.is(":hidden")) $table.show();
            // Add as editable row
            const $row = buildZoneRow(gz.num, gz.data, group.hasHwRt, bypassStates[String(gz.num)]);
            $tbody.append($row);
            $(this).remove();
            // Hide section if no more unused badges
            if ($unused.find(".zone-unused-badge").length === 0) $unused.hide();
            sortTableRows($tbody);
          });
          $unused.append($badge);
        });

        $body.append($unused);
      }

      // Toggle collapse/expand
      $header.on("click", function () {
        $(this).toggleClass("collapsed");
        $body.slideToggle(200);
      });

      $card.append($header, $body);
      $container.append($card);
    });

    // Non-blocking bypass sync so UI appears instantly after refresh.
    api("GET", "/api/zone_states")
      .then(bs => {
        if (renderToken !== _zonesRenderToken) return;
        const states = (bs && bs.zones) || {};
        for (let z = 1; z <= 48; z++) {
          const val = states[String(z)];
          if (val !== undefined && val !== null) updateBypassCell(z, val);
        }
      })
      .catch(() => {});

    // If BYPASS is active, a direct panel scan gives the most accurate zone list.
    setTimeout(() => {
      if (renderToken !== _zonesRenderToken) return;
      scanBypassFromPanel({ silent: true });
    }, 600);
  }

  $(document).on("configLoaded", (_evt, config) => renderZones(config));
  if (APP.config) renderZones(APP.config);

  // ── Live bypass tracking ──
  // When a display event arrives with a changed bypass attribute (e.g. another
  // keypad bypassed/unbypassed a zone), refresh the bypass cells in the zones
  // tab immediately so the UI stays in sync.
  let _lastBypassAttr = null;
  $(document).on("snifferDisplay", function (_evt, display, attrs) {
    if (!attrs) return;
    const bypassNow = !!attrs.armed_bypass;
    if (_lastBypassAttr === null) {
      // Page loaded while BYPASS already active: no transition event occurs,
      // so proactively sync bypassed zones once on first display message.
      if (bypassNow) setTimeout(() => scanBypassFromPanel({ silent: true }), 500);
      _lastBypassAttr = bypassNow;
      return;
    }
    if (_lastBypassAttr !== null && _lastBypassAttr !== bypassNow) {
      // Bypass LED state changed — a zone was bypassed or cleared.
      // Skip if UI is locked (bypassAction is already handling the scan).
      if (!$("#busy-overlay").hasClass("hidden")) {
        _lastBypassAttr = bypassNow;
        return;
      }
      if (!bypassNow) {
        // BYPASS LED went OFF → all bypasses cleared.  Just reset all cells.
        for (let z = 1; z <= 48; z++) updateBypassCell(z, false);
      } else {
        // BYPASS LED went ON → a zone was bypassed.  Read which ones.
        setTimeout(scanBypassFromPanel, 2000);
      }
    }
    _lastBypassAttr = bypassNow;
  });
});
