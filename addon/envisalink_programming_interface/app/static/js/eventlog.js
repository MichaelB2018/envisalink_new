/**
 * eventlog.js — Event Log tab
 *
 * Sends POST /api/eventlog, then displays entries in a 3-column table:
 *   # | Raw panel string | Decoded Contact ID description
 */

"use strict";

// Ademco / Contact ID event code descriptions (Vista 20P manual, Table of CID Event Codes)
const CID_CODES = {
  100: "Medical Alarm",        101: "Personal Emergency",   110: "Fire Alarm",
  111: "Smoke/Fire",           113: "Water / Flood",        120: "Panic Alarm",
  121: "Duress",               122: "Alarm, 24-Hour Silent",
  123: "Alarm, 24-Hour Audible",
  130: "Burglary",             131: "Alarm, Perimeter",     132: "Interior Alarm",
  133: "24-Hr Zone",           134: "Alarm, Entry/Exit",
  135: "Alarm, Day/Night",     136: "Outdoor Alarm",        137: "Zone Tamper",
  138: "Near Alarm",           140: "General Alarm",        141: "Polling Trouble",
  143: "Alarm, Expansion Module",
  145: "ECP Module Cover Tamper",
  146: "Silent Burglary",
  150: "Alarm, 24-Hour Auxiliary/Monitor Zone",
  151: "Gas Detected",         154: "Water Level",
  158: "High Temperature",     159: "Low Temperature",      162: "Carbon Monoxide",
  200: "Fire Supervisory",
  300: "System Trouble",       301: "AC Power",
  302: "Low System Battery/Battery Test Fail",
  305: "System Reset (Log only)",
  306: "Program Changed",      307: "Self-Test Failure",
  309: "Battery Missing",      311: "Battery Low",
  320: "Sounder Trouble",      321: "Bell/Siren Trouble",
  333: "Trouble, Expansion Mod. Supervision",
  341: "Trouble, ECP Cover Tamper",
  344: "RF Receiver Jam",
  350: "Communication Trouble", 351: "Telco Line Fault",
  353: "Long Range Radio Trouble",
  354: "Failure to Communicate (Log only)",
  373: "Fire Loop Trouble",    374: "Exit Error Alarm",
  380: "Global Trouble, Trouble Day/Night",
  381: "RF Sensor Supervision",
  382: "Supervision Auxiliary Wire Zone",
  383: "RF Sensor Tamper",     384: "RF Sensor Low-Battery",
  393: "Clean Me",
  401: "Disarmed, Armed AWAY, Armed MAXIMUM",
  403: "Schedule Arm/Disarm AWAY",
  406: "Cancel by User",       407: "Remote Arm/Disarm (Downloading)",
  408: "Quick Arm AWAY",       409: "Keyswitch Arm/Disarm AWAY",
  420: "Close",                421: "Group Close",
  422: "Auto Close",           423: "Late Close",
  441: "Disarmed/Armed STAY/INSTANT, Quick-Arm STAY/INSTANT",
  442: "Keyswitch Arm/Disarm STAY",
  455: "Scheduled Arm Fail",   456: "Partial Arm",
  457: "Exit Error",           459: "Recent Closing (SIA panels only)",
  461: "Wrong Code",           471: "Armed by Keyswitch",
  570: "Bypass",               571: "Fire Bypass",
  572: "24-Hr Zone Bypass",    573: "Burg Bypass",
  601: "Manually Triggered Dialer Test",
  602: "Periodic Test",        606: "AAV to Follow",
  607: "Walk Test Entered/Exited",
  608: "System Test",
  621: "Manual Trigger Test",  622: "Auto Test",
  623: "Event Log 80% Full",
  625: "Real-Time Clock Changed (Log only)",
  626: "Test End",
  627: "Program Mode Entry (Log only)",
  628: "Program Mode Exit (Log only)",
  636: "24-Hr Inspection Fail", 641: "Senior Watch Trouble",
  642: "Latch Key (Log only)",
};

/**
 * Parse a Vista 20P event log string.
 *
 * The EVL returns the 32-char LCD alpha field which may be split as two
 * 16-char lines joined by '\n' or concatenated directly:
 *   Line 1: "013 E627 U000 P0"
 *   Line 2: "08:42PM 03/29/26"
 *
 * Normalise by replacing any newline (or \r) with a space so the regex
 * handles both forms.
 *
 * Format after normalisation: "NNN [E|R]CCC [U|Z]NNN P{P} HH:MMam/pm MM/DD/YY"
 * E = Event (open/alarm)  R = Restore/Close
 * The field after the CID code is U (user) for arm/disarm events and
 * Z (zone) for zone/trouble events — both must be accepted.
 */
function decodeEvent(raw) {
  if (!raw) return "";
  // Normalise LCD line break and any extra whitespace
  const s = raw.replace(/[\r\n]+/g, " ").replace(/\s{2,}/g, " ").trim();
  // P(\d) — partition is a single digit (0-8 on Vista 20P).  Must NOT be
  // greedy (\d+) because the time may follow with no space, e.g.
  // "P109:28PM" is partition 1, time 09:28PM — not partition 10.
  // [UZ] — U = user number (arm events), Z = zone number (trouble/alarm events)
  const m = s.match(/^(\d+)\s+([ER])(\d{3})\s+([UZ])(\d+)\s+P(\d)\s*(\d{1,2}:\d{2}(?:AM|PM))\s+(\S+)/i);
  if (!m) return "—";
  const qualifier = m[2].toUpperCase() === "E" ? "Event" : "Restore";
  const codeNum   = parseInt(m[3], 10);
  const uzType    = m[4].toUpperCase();
  const uzNum     = parseInt(m[5], 10);
  const partition = parseInt(m[6], 10);
  const time      = m[7];
  const date      = m[8];
  const name      = CID_CODES[codeNum] || `Code ${m[3]}`;
  const uzLabel   = uzType === "U" ? "User" : "Zone";
  const zoneStr   = uzNum > 0 ? ` · ${uzLabel} ${uzNum}` : "";
  const partStr   = partition > 0 ? ` · Part ${partition}` : "";
  return `${qualifier}: ${name}${zoneStr}${partStr} @ ${time} ${date}`;
}

$(function () {

  let _fetching = false;

  $("#btn-fetch-log").on("click", async function () {
    if (_fetching) return;
    if (!APP.codeSet) { toast("Set installer code first", "err"); return; }
    if (!APP.userCode) { toast("Set user/master code in the login modal first", "err"); return; }

    const count = parseInt($("#log-count-select").val(), 10) || 5;
    _fetching = true;

    const $btn    = $(this).prop("disabled", true);
    const $status = $("#log-status").text("⟳ Fetching from panel…").removeClass("hidden");
    const $table  = $("#log-table").addClass("hidden");
    const $tbody  = $("#log-tbody").empty();
    lockUI("Fetching event log from panel…");

    try {
      const res = await api("POST", "/api/eventlog", { entries: count });
      const entries = res.entries || [];

      if (!entries.length) {
        $status.text("No entries returned.").removeClass("hidden");
      } else {
        entries.forEach((entry, idx) => {
          const rawHtml  = $("<span>").text(entry).html();
          const decoded  = decodeEvent(entry);
          $tbody.append(
            `<tr>
               <td>${idx + 1}</td>
               <td class="log-raw">${rawHtml}</td>
               <td class="log-decoded">${$("<span>").text(decoded).html()}</td>
             </tr>`
          );
        });
        $table.removeClass("hidden");
        $status.addClass("hidden");
        toast(`Fetched ${entries.length} log ${entries.length === 1 ? "entry" : "entries"}`, "ok");
      }
    } catch (e) {
      $status.text(`Error: ${e.message}`);
      toast(`Event log error: ${e.message}`, "err");
    } finally {
      unlockUI();
      $btn.prop("disabled", false);
      _fetching = false;
    }
  });
});

