/**
 * keypad.js — Virtual Keypad tab
 *
 * Layout matches the real Honeywell 6150 keypad:
 *   Left column : STAY / AWAY / POLICE / PAGE function keys (visual only –
 *                 these are hardware-level buttons not available via TPI)
 *   Numeric grid: 3-column × 4-row with secondary function labels
 *   Bottom row  : EXIT PROG shortcut
 */

"use strict";

$(function () {

  // Left-column function keys – decorative (TPI cannot simulate hold-buttons)
  const FN_KEYS = [
    { label: "STAY",   title: "Hardware key – not available via TPI" },
    { label: "AWAY",   title: "Hardware key – not available via TPI" },
    { label: "POLICE", title: "Hardware key – not available via TPI" },
    { label: "PAGE",   title: "Hardware key – not available via TPI" },
  ];

  // 4 rows × 3 cols, matching the physical 6150 layout
  const GRID = [
    [{ k: "1", sub: "OFF"     }, { k: "2", sub: "AWAY"    }, { k: "3", sub: "STAY"   }],
    [{ k: "4", sub: "MAX"     }, { k: "5", sub: "TEST"    }, { k: "6", sub: "BYPASS" }],
    [{ k: "7", sub: "INSTANT" }, { k: "8", sub: "CODE"    }, { k: "9", sub: "CHIME"  }],
    [{ k: "*", sub: "READY"   }, { k: "0", sub: ""        }, { k: "#", sub: ""        }],
  ];

  function buildKeypad() {
    const $root = $("#keypad-root").empty();

    const $wrapper = $('<div class="kpad-wrapper"></div>');

    // ── Left function column ──
    const $fnCol = $('<div class="kpad-fn-col"></div>');
    FN_KEYS.forEach(({ label, title }) => {
      $fnCol.append(
        $(`<button class="kpad-fn-btn" title="${title}" disabled>${label}</button>`)
      );
    });
    $wrapper.append($fnCol);

    // ── Numeric grid ──
    const $numeric = $('<div class="kpad-numeric"></div>');
    GRID.forEach(row => {
      const $row = $('<div class="kpad-row"></div>');
      row.forEach(({ k, sub }) => {
        const isSpecial = k === "*" || k === "#";
        $row.append(
          $(`<button class="kpad-btn${isSpecial ? " special" : ""}" data-keys="${k}">
               <span class="kpad-main">${k}</span>
               ${sub ? `<span class="kpad-sub">${sub}</span>` : ""}
             </button>`)
        );
      });
      $numeric.append($row);
    });
    $wrapper.append($numeric);

    $root.append($wrapper);

    // ── Bottom shortcut row ──
    $root.append(
      $('<div class="kpad-shortcuts"></div>').append(
        $('<button class="kpad-shortcut" data-keys="*99" title="Exit programming mode">EXIT PROG</button>'),
        $('<button class="kpad-shortcut kpad-recovery" title="Send full recovery sequence: *00 / 0 / 00* / *99&#10;Use if a failed scan left the panel stuck in programming mode">RECOVERY</button>')
      )
    );
  }

  async function sendKey(keys) {
    try {
      await api("POST", "/api/keypress", { keys });
    } catch (e) {
      toast(`Keypress failed: ${e.message}`, "err");
    }
  }

  $("#keypad-root").on("click", ".kpad-btn, .kpad-shortcut", function () {
    const keys = $(this).data("keys");
    if (keys !== undefined) {
      sendKey(String(keys));
      return;
    }
    // Recovery button has no data-keys; calls the server recovery endpoint instead
    if ($(this).hasClass("kpad-recovery")) {
      api("POST", "/api/recovery")
        .then(() => toast("Recovery sequence sent", "ok"))
        .catch(e => toast(`Recovery failed: ${e.message}`, "err"));
    }
  });

  $(document).on("keydown", function (e) {
    if ($("#tab-keypad").hasClass("active")) {
      const k = e.key;
      if (/^[0-9*#]$/.test(k)) {
        e.preventDefault();
        sendKey(k);
      }
    }
  });

  buildKeypad();
});

