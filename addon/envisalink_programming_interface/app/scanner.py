"""
Panel configuration scanner.

Reads current zone types, zone names, and system delay fields from the
Vista 20P by navigating through the panel's programming menus via
send_and_capture() and parsing the keypad display text.

All scan sequences are designed to be read-only (#field = review mode,
no changes accepted). Zone type reading uses *58 expert mode which shows
a compact summary line without requiring data entry.

After scanning, the result is persisted to /data/config_cache.json.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import re
import time
from typing import Any

from .ha_client import HAClient
from .panel_commands import VOCAB, REPORTING_FIELDS

_LOGGER = logging.getLogger(__name__)

CACHE_PATH = "/data/config_cache.json"
SCAN_LOG_PATH = "/data/scan_log.json"
# Zones 1-8 are the hardwired zones on the Vista 20P/15P.
HARDWIRED_ZONES = list(range(1, 9))
# All supported zones: 1-8 hardwired, 9-16 expansion, 17-48 wireless/expansion.
MAX_ZONE = 48
ALL_ZONES = list(range(1, MAX_ZONE + 1))

# Inter-keystroke delay (seconds) between characters within one step.
# Must be long enough for the EVL to ACK each command before the next.
CHAR_DELAY = 0.25

# Delay (seconds) before capturing display after the final character.
# Gives the panel time to render the response.
STEP_DELAY = 0.50

# If the panel doesn't respond within the client's CAPTURE_TIMEOUT,
# wait up to this many additional seconds for the display to stabilise before
# declaring the scan aborted.  Covers brief disconnects / bus congestion.
PANEL_RECOVERY_WAIT = 60.0

# Total capture steps (base, without panel time):
#   1 enter prog + 5 delay field reads              = 6
#   1 *58 entry + 47 zone type reads                = 48
#   1 *82 entry (= zone 1) + 47 *NN navigations    = 48
#   12 custom word reads (words 01-12)              = 12
#   1 enter prog + N reporting field reads          = 1 + N
#   1 enter prog + 7 keypad field reads             = 8
# One additional step is added when user_code is provided (View Time/Date).
TOTAL_SCAN_STEPS = 6 + 48 + 48 + 12 + (1 + len(REPORTING_FIELDS)) + 8

# Step counts per section — used for partial-scan progress bars.
_SECTION_STEPS = {
    "zones": 12 + 48 + 48,     # custom words (12) + *58 (48 zones) + *82 (48 zone names)
    "words": 12,                # *82 custom word mode: words 01-12
    "system": 6,                # 1 (enter prog) + 5 field reads
    "reporting": 1 + len(REPORTING_FIELDS),  # 1 (enter prog) + N field reads
    "keypads": 8,               # 1 (enter prog) + 7 keypad field reads (*190-*196)
}


class ScanAbortError(Exception):
    """Raised when a scan step receives an unexpected/empty response and the
    panel does not recover within PANEL_RECOVERY_WAIT seconds.  Signals that
    further keypresses must not be sent to avoid corrupting panel data."""
    pass


# Substrings that indicate the panel is in normal (non-programming) mode.
# If any of these appear in a captured display during a scan, the panel
# has left programming mode — usually due to an EVL disconnect/reconnect
# or an installer-code timeout.  The scan must abort immediately.
_NORMAL_MODE_INDICATORS = (
    "DISARMED",
    "ARMED",
    "Ready to Arm",
    "Not Ready",
    "FAULT",
    "May Exit Now",
    "FIRE",
    "ALARM",
    "CHECK",
)


def _is_normal_mode_display(display: str) -> bool:
    """Return True if the display text indicates the panel is in normal
    (non-programming) mode."""
    if not display:
        return False
    return any(indicator in display for indicator in _NORMAL_MODE_INDICATORS)


class PanelScanner:
    def __init__(self, client: HAClient) -> None:
        self._client = client
        self._scanning = False
        self._scan_step = 0
        self._scan_total = TOTAL_SCAN_STEPS
        self._on_progress: Any = None
        # Running log for the most recent scan; list of dicts
        self._scan_log: list[dict] = []

    @property
    def scanning(self) -> bool:
        return self._scanning

    async def _progress(self, msg: str) -> None:
        """Increment step counter and fire progress callback."""
        self._scan_step += 1
        if self._on_progress:
            try:
                await self._on_progress(self._scan_step, self._scan_total, msg)
            except Exception:
                pass

    def _log(self, level: str, step: str, keys: str, display: str, note: str = "") -> None:
        """Append an entry to the scan log."""
        entry = {
            "t": round(time.time(), 2),
            "level": level,       # "ok" | "warn" | "error"
            "step": step,
            "keys": keys,
            "display": display,
            "note": note,
        }
        self._scan_log.append(entry)
        log_fn = _LOGGER.warning if level == "warn" else (
            _LOGGER.error if level == "error" else _LOGGER.debug
        )
        log_fn("SCAN [%s] keys=%r display=%r%s", step, keys, display,
               f" ({note})" if note else "")

    async def _wait_for_display_change(
        self, initial: str, max_wait: float = 15.0, poll_interval: float = 0.4
    ) -> str:
        """
        Poll get_current_display until the display changes from `initial`.
        Used after captures where the panel auto-updates a moment later
        (e.g. #34 shows the field name first, then the actual value ~2s later).
        Returns the new display, or `initial` on timeout.
        """
        deadline = asyncio.get_event_loop().time() + max_wait
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(poll_interval)
            current = await self._client.get_current_display()
            if current and current != initial:
                elapsed = asyncio.get_event_loop().time() - start
                _LOGGER.debug(
                    "_wait_for_display_change: changed after %.1fs => %r", elapsed, current
                )
                return current
        _LOGGER.debug("_wait_for_display_change: timed out after %.1fs, still %r", max_wait, initial)
        return initial

    def _assert_still_in_programming(self, display: str, step: str) -> None:
        """Raise ScanAbortError if the display indicates normal mode.

        After an EVL disconnect/reconnect, the panel returns to its idle
        display (e.g. "****DISARMED****  Ready to Arm").  Continuing to
        send programming-mode commands would be futile and potentially
        harmful.  Detect this early and abort.
        """
        if _is_normal_mode_display(display):
            msg = (
                f"Panel left programming mode at step '{step}' "
                f"(display: {display!r}). Likely EVL disconnect/reconnect. "
                "Scan aborted."
            )
            _LOGGER.error(msg)
            self._log("error", step, "", display,
                      "panel left programming mode — aborting")
            raise ScanAbortError(msg)

    async def _capture_with_fallback(
        self, keys: str, progress_msg: str, *, expect_programming: bool = True,
    ) -> str:
        """
        Send a keypress sequence and capture the panel display that results.

        IMPORTANT: sending a multi-char string to send_and_capture() causes it
        to capture the FIRST display update while the remaining characters are
        still queued/in-flight.  Those trailing chars then arrive at the panel
        in the wrong state, corrupting subsequent steps and zone descriptors.

        Fix: send every character individually with CHAR_DELAY between each,
        so the panel fully processes each keystroke before the next arrives.
        Only the final character uses send_and_capture so we get exactly the
        display that results from completing the full sequence.

        If *expect_programming* is False, the programming-mode assertion is
        skipped (used for normal-mode commands like View Time/Date).

        Raises ScanAbortError if the panel does not respond after the initial
        timeout AND fails to produce any display change within
        PANEL_RECOVERY_WAIT seconds, OR if the display indicates the panel
        has left programming mode (e.g. after an EVL disconnect/reconnect).
        """
        await self._progress(progress_msg)
        # Snapshot BEFORE any keys — used as the baseline to detect whether
        # the panel responded to ANY of the keys in this step.  Critical for
        # *58 zone navigation where the intermediate digits (e.g. "02") already
        # update the display and the final key ("#") doesn't change it again.
        display_before_keys = await self._client.get_current_display()
        for ch in keys[:-1]:
            await asyncio.sleep(CHAR_DELAY)
            await self._client.send_keypress(ch)
        await asyncio.sleep(STEP_DELAY)
        # Snapshot AFTER intermediate chars — may already reflect the result
        # (e.g. zone 2 data shown after sending "02" in *58 mode).
        display_before_capture = await self._client.get_current_display()

        # Build a predicate that rejects (a) the pre-KEYS baseline AND
        # (b) any intermediate "Field?" transit display.  Uses
        # display_before_keys (not display_before_capture) so that results
        # which appear during intermediate keys aren't incorrectly filtered.
        _baseline = display_before_keys
        _LOGGER.debug(
            "CAPTURE [%s] pre_keys=%r  pre_capture=%r  final_key=%r",
            progress_msg, display_before_keys, display_before_capture, keys[-1],
        )

        def _ignore(d: str) -> bool:
            return d == _baseline or "Field?" in d

        display = await self._client.send_and_capture(
            keys[-1], ignore_display=_ignore, timeout=3.0,
        )

        if display is not None:
            if expect_programming:
                self._assert_still_in_programming(display, progress_msg)
            self._log("ok", progress_msg, keys, display)
            return display

        # ----------------------------------------------------------------
        # WS capture timed out after 3 s.  HA WebSocket event delivery is
        # unreliable — events are frequently delayed or lost entirely.
        # Fall back to REST polling which reads the HA entity state directly.
        #
        # Fast REST poll: check every 0.4 s for up to 12 s (total ~15 s
        # from the original keypress).  Most panel responses land within
        # 1–2 s, so this resolves the majority of missed-WS-event cases
        # with only 3–4 s total delay instead of a full 15 s timeout.
        # ----------------------------------------------------------------
        rest_display = await self._wait_for_display_change(
            display_before_keys, max_wait=12.0, poll_interval=0.4,
        )
        if rest_display and not _ignore(rest_display):
            if expect_programming:
                self._assert_still_in_programming(rest_display, progress_msg)
            elapsed = 3.0  # WS timeout already elapsed
            self._log("ok", progress_msg, keys, rest_display)
            _LOGGER.debug(
                "CAPTURE [%s] WS missed, REST poll found result: %r",
                progress_msg, rest_display,
            )
            return rest_display

        # ----------------------------------------------------------------
        # Neither WS (3 s) nor REST polling (12 s) detected a display
        # change.  The panel may be disconnected or genuinely confused.
        # Wait up to PANEL_RECOVERY_WAIT for any display change before
        # aborting.  Do NOT send keypresses during this wait.
        # ----------------------------------------------------------------
        warn_msg = (
            f"No display update within ~15s for step '{progress_msg}' "
            f"(keys={keys!r}). Panel may be slow or disconnected. "
            f"Waiting up to {PANEL_RECOVERY_WAIT:.0f}s for recovery."
        )
        _LOGGER.warning(warn_msg)
        self._log("warn", progress_msg, keys, "",
                  "no display update in ~15s — waiting for panel to respond")

        # Notify the UI about the delay
        if self._on_progress:
            try:
                await self._on_progress(
                    self._scan_step, self._scan_total,
                    f"⏳ Panel not responding \u2014 waiting up to {PANEL_RECOVERY_WAIT:.0f}s\u2026"
                )
            except Exception:
                pass

        # Poll for any display change (panel reconnecting)
        pre_recovery = await self._client.get_current_display()

        # ----------------------------------------------------------------
        # Race: display arrived WHILE the logging/notification code above
        # was running — so the immediate `current` check still saw the old
        # display, but by the time we read `pre_recovery` the new display
        # is already there.  Detect this by comparing against
        # display_before_capture (the pre-keystroke baseline).
        # ----------------------------------------------------------------
        if pre_recovery and not _ignore(pre_recovery):
            if expect_programming:
                self._assert_still_in_programming(pre_recovery, progress_msg)
            _LOGGER.warning(
                "SCAN [%s] display changed during recovery setup phase "
                "(arrived between timeout check and recovery poll start). "
                "Accepted late result: %r",
                progress_msg, pre_recovery,
            )
            self._log("warn", progress_msg, keys, pre_recovery,
                      "display changed during recovery setup — late result accepted")
            return pre_recovery

        recovery_start = asyncio.get_event_loop().time()
        recovered_display = await self._wait_for_display_change(
            pre_recovery, max_wait=PANEL_RECOVERY_WAIT, poll_interval=1.0
        )
        recovery_elapsed = asyncio.get_event_loop().time() - recovery_start

        if recovered_display and not _ignore(recovered_display):
            if expect_programming:
                self._assert_still_in_programming(recovered_display, progress_msg)
            # The panel responded during the recovery wait.  Because the scanner
            # holds the panel in installer programming mode for the entire scan,
            # unsolicited partition/zone state changes are suppressed — any
            # display change here must be the (very late) response to our keys.
            # Accept it with a warning rather than aborting.
            _LOGGER.warning(
                "SCAN [%s] panel responded %.1fs into recovery wait (very slow response). "
                "Accepted late result: %r",
                progress_msg, recovery_elapsed, recovered_display,
            )
            self._log("warn", progress_msg, keys, recovered_display,
                      f"very slow panel: display arrived {recovery_elapsed:.1f}s into recovery wait — late result accepted")
            return recovered_display

        # No change at all — panel still not responding.
        # Final safety net: the display may have changed to the expected
        # value BEFORE _wait_for_display_change started (its 'initial'
        # already equalled the new data, so it never detected a "change").
        # Check one last time against the _ignore predicate.
        fallback = await self._client.get_current_display()
        if fallback and not _ignore(fallback):
            if expect_programming:
                self._assert_still_in_programming(fallback, progress_msg)
            _LOGGER.warning(
                "SCAN [%s] display is valid at final check (was already "
                "present before recovery poll started). Accepted: %r",
                progress_msg, fallback,
            )
            self._log("warn", progress_msg, keys, fallback,
                      "display valid at final safety-net check — accepted")
            return fallback
        self._log("error", progress_msg, keys, fallback or "",
                  "panel unresponsive after recovery wait — aborting")
        await self._flush_log()
        raise ScanAbortError(
            f"Panel unresponsive for {PANEL_RECOVERY_WAIT:.0f}s at step '{progress_msg}'. "
            "Stopped to prevent data corruption."
        )

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def scan_all(
        self, code: str, user_code: str = "", on_progress: Any = None
    ) -> dict[str, Any]:
        """
        Full configuration scan.  Returns dict and saves to cache.
        on_progress: optional async callable(step, total, msg) fired after each capture.
        user_code: master/user code for View Time/Date ({user_code}#63).  If empty,
        the panel time step is skipped and panel_time is None in the result.
        Raises ScanAbortError (subclass of RuntimeError) if the panel stops
        responding mid-scan.  Caller must show the user a retry prompt.
        """
        if self._scanning:
            raise RuntimeError("Scan already in progress")
        self._scanning = True
        self._scan_step = 0
        self._scan_total = TOTAL_SCAN_STEPS + (1 if user_code else 0)
        self._on_progress = on_progress
        self._scan_log = []  # reset log for this run
        result: dict[str, Any] = {
            "scanned_at": time.time(),
            "delays": {},
            "zones": {},
            "custom_words": {},
            "reporting": {},
            "keypads": {},
            "panel_time": None,
        }
        abort_error: ScanAbortError | None = None
        try:
            result["delays"] = await self._scan_delays(code)
            result["custom_words"] = await self._scan_custom_words(code)
            result["zones"] = await self._scan_zones(code)
            result["reporting"] = await self._scan_reporting(code)
            result["keypads"] = await self._scan_keypads(code)
            if user_code:
                result["panel_time"] = await self._scan_panel_time(user_code)
            await self._save_cache(result)
        except ScanAbortError as exc:
            abort_error = exc
            # Save whatever partial data was collected so the UI isn't left
            # showing stale zone_type values (e.g. all-Perimeter defaults).
            if result["delays"] or result["zones"]:
                await self._save_cache(result)
        finally:
            # Best-effort recovery: exit any open programming menu.
            # Sequence handles being stuck in *82 zone-browse, *82 PROGRAM ALPHA?,
            # *58 zone entry, or at the top-level data-field prompt.
            await self.force_exit()
            await self._flush_log()
            self._scanning = False
            self._on_progress = None
        if abort_error is not None:
            raise abort_error
        return result

    async def scan_section(
        self, code: str, section: str, on_progress: Any = None
    ) -> dict[str, Any]:
        """
        Partial scan — re-read just one section ('zones' or 'system').
        Merges the result into the existing cache and returns the full config.
        """
        if section not in _SECTION_STEPS:
            raise ValueError(f"Unknown section: {section!r}")
        if self._scanning:
            raise RuntimeError("Scan already in progress")
        self._scanning = True
        self._scan_step = 0
        self._scan_total = _SECTION_STEPS[section]
        self._on_progress = on_progress
        self._scan_log = []

        # Load existing cache so we can merge the partial result
        cached = await self.load_cache() or {
            "scanned_at": time.time(),
            "delays": {},
            "zones": {},
            "custom_words": {},
            "reporting": {},
            "keypads": {},
            "panel_time": None,
        }
        abort_error: ScanAbortError | None = None
        try:
            if section == "zones":
                cached["custom_words"] = await self._scan_custom_words(code)
                cached["zones"] = await self._scan_zones(code)
            elif section == "words":
                cached["custom_words"] = await self._scan_custom_words(code)
            elif section == "system":
                cached["delays"] = await self._scan_delays(code)
            elif section == "reporting":
                cached["reporting"] = await self._scan_reporting(code)
            elif section == "keypads":
                cached["keypads"] = await self._scan_keypads(code)
            cached["scanned_at"] = time.time()
            await self._save_cache(cached)
        except ScanAbortError as exc:
            abort_error = exc
            await self._save_cache(cached)
        finally:
            await self.force_exit()
            await self._flush_log()
            self._scanning = False
            self._on_progress = None
        if abort_error is not None:
            raise abort_error
        return cached

    async def force_exit(self) -> None:
        """
        Best-effort recovery: send sequences that exit any known
        Vista 20P programming menu state back to normal operating mode.

        Covers these states (in order of recovery):
          *58 zone display  : * → "Enter * or #" → * → "Field?" → *99
          *82 zone-browse   : *00 → PROGRAM ALPHA? → 0 → data-field → *99
          PROGRAM ALPHA?    : 0 → data-field → *99
          Data-field prompt : *99 → normal mode

        Safe to call even when the panel is already in normal operating mode
        (the sequences are either ignored or produce benign EE/OC displays).

        Each step is a single keypress string with a delay to let the panel
        process the input before the next arrives.
        """
        for seq in ("*", "*", "00", "0", "*99"):
            try:
                await self._client.send_keypress(seq)
            except Exception:
                pass
            await asyncio.sleep(STEP_DELAY)

    async def load_cache(self) -> dict[str, Any] | None:
        """Load and return cached config, or None if no cache exists."""
        if not os.path.exists(CACHE_PATH):
            return None
        try:
            with open(CACHE_PATH, "r") as f:
                return json.load(f)
        except Exception as exc:
            _LOGGER.warning("Failed to load cache: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Delay scanning
    # ------------------------------------------------------------------

    async def _scan_delays(self, code: str) -> dict[str, Any]:
        """
        Read fire timeout (*32), bell timeout (*33), exit delay (*34),
        entry delay 1 (*35), entry delay 2 (*36).
        Uses review mode (#field) — read-only, no data accepted.

        Stays in programming mode for the entire batch: enters once with
        *99{code}800, reviews each field with #NN, and exits with *99 at
        the end.  For scrolling fields (*34-*36, *43) digit pairs are
        collected via _collect_scrolling_digits.  For simple fields (*32,
        *33) a single _wait_for_display_change captures the value, then
        '#' dismisses the review back to the Field? prompt.
        """
        delays = {}
        # (key, field_num, label, scrolling, max_digits, parser_for_simple)
        field_map = [
            ("fire_timeout",    32, "No Fire Timeout",   False, 2,  _parse_fire_timeout_display),
            ("bell_timeout",    33, "Bell Timeout",      False, 2,  _parse_delay_display),
            ("exit_delay",      34, "Exit Delay 1 2",    True,  4,  None),
            ("entry_delay_1",   35, "Entry 1 Dly 1 2",   True,  4,  None),
            ("entry_delay_2",   36, "Entry 2 Dly 1 2",   True,  4,  None),
        ]

        # Enter programming mode once
        entry_display = await self._capture_with_fallback(
            f"*99{code}800", "Entering programming mode"
        )
        await self._wait_for_display_change(entry_display, max_wait=5.0)

        for name, field, label, scrolling, max_digits, parser in field_map:
            display = await self._capture_with_fallback(
                f"#{field:02d}", f"Reading {label} (*{field})"
            )
            _LOGGER.info("scan_delays %s #%d capture=%r scrolling=%s",
                         name, field, display, scrolling)

            if scrolling:
                value = await self._collect_scrolling_digits(
                    display, field, max_digits, raw=True
                )
                _LOGGER.info("scan_delays %s #%d scrolling_result=%r",
                             name, field, value)
                # Scrolling fields auto-return to "Field?" when done
            else:
                # Simple field: panel shows field-label + number, then
                # auto-updates to the stored value ~2 s later.
                value_display = await self._wait_for_display_change(
                    display, max_wait=15.0
                )
                value = parser(value_display)
                # Dismiss the review to get back to "Field?" prompt
                await self._client.send_keypress("#")
                await asyncio.sleep(STEP_DELAY)

            delays[name] = value
            _LOGGER.info("Scanned %s (*%d) = %r", name, field, value)

        # Exit programming mode
        await self._client.send_keypress("*99")
        await asyncio.sleep(STEP_DELAY)
        return delays

    # ------------------------------------------------------------------
    # Zone scanning
    # ------------------------------------------------------------------

    async def _scan_zones(self, code: str) -> dict[str, Any]:
        """
        Read zone type (*58) and zone name (*82) for zones 1-48.

        Zones 1-8:   Hardwired on-board (have HW type + response time)
        Zones 9-16:  Expansion zones
        Zones 17-48: Wireless / expansion zones

        Zone types (*58 expert mode):
          One session: enter *58, answer '1' (yes) to SET TO CONFIRM? — panel
          immediately shows zone 01 data.  Navigate zones 2-48 with "{ZZ}#"
          ('#' = back without saving, read-only).  Exit with "00*" (→ "Enter *
          or #" prompt), then "*" (→ Field?), then "*99".

        Zone names (*82 alpha programming):
          One session: enter *82, say yes (1) to PROGRAM ALPHA?,
          say no (0) to custom words → panel shows zone 1 descriptor.
          Navigate each subsequent zone with "*NN" (browse mode, no cursor).
          Exit with "*00" (zone 00 sentinel → PROGRAM ALPHA? prompt),
          then "0" (exit alpha → data-field mode), then "*99".

          KEY: never send bare "*99" while inside *82 — within that menu
          "*99" is parsed as "navigate to zone 99", not as an exit command.
        """
        zones: dict[str, Any] = {str(z): {"zone": z} for z in ALL_ZONES}

        # --- zone types: one *58 session, navigate each zone by number ---
        # Send setup keys (*99{code}800*58) as plain keypresses so that
        # intermediate displays ("Field?", "SET TO CONFIRM?") are consumed
        # before the capture starts.  Only the final "1" (YES to confirm)
        # triggers the actual Zone 1 type display.  Fixes HA-mode race
        # where delayed WS events from intermediate prompts were captured
        # instead of the Zone 1 result.
        setup_58 = f"*99{code}800*58"
        for ch in setup_58:
            await asyncio.sleep(CHAR_DELAY)
            await self._client.send_keypress(ch)
        await asyncio.sleep(STEP_DELAY)
        entry_type_display = await self._capture_with_fallback(
            "1", "Zone 1: reading type (*58)"
        )
        zones["1"].update(_parse_zone_type_display(entry_type_display))
        zones["1"]["raw_type_display"] = entry_type_display

        for zone in range(2, MAX_ZONE + 1):
            display = await self._capture_with_fallback(
                f"{zone:02d}#", f"Zone {zone}: reading type (*58)"
            )
            zones[str(zone)].update(_parse_zone_type_display(display))
            zones[str(zone)]["raw_type_display"] = display

        _LOGGER.info("SCAN [exit *58] sending 00* * *99")
        await self._client.send_keypress("00*")
        await asyncio.sleep(STEP_DELAY)
        await self._client.send_keypress("*")
        await asyncio.sleep(STEP_DELAY)
        await self._client.send_keypress("*99")
        await asyncio.sleep(STEP_DELAY)

        # --- zone names: one *82 session, navigate each zone explicitly ---
        # Send setup keys (*99{code}800*821) as plain keypresses so that
        # intermediate displays ("PROGRAM ALPHA?", "Custom Words? 0=No,1=Yes")
        # are consumed before the capture starts.  Only the final "0" (NO to
        # custom words) triggers the actual Zone 1 name display.  In HA mode,
        # delayed WS events from these intermediate prompts were arriving
        # during send_and_capture and being accepted as the Zone 1 result.
        setup_82 = f"*99{code}800*821"
        for ch in setup_82:
            await asyncio.sleep(CHAR_DELAY)
            await self._client.send_keypress(ch)
        await asyncio.sleep(STEP_DELAY)
        entry_display = await self._capture_with_fallback(
            "0", "Zone 1: reading name (*82)"
        )
        zones["1"]["name"] = _parse_zone_name_display(entry_display)
        zones["1"]["raw_name_display"] = entry_display

        for zone in range(2, MAX_ZONE + 1):
            display = await self._capture_with_fallback(
                f"*{zone:02d}", f"Zone {zone}: reading name (*82)"
            )
            zones[str(zone)]["name"] = _parse_zone_name_display(display)
            zones[str(zone)]["raw_name_display"] = display

        _LOGGER.info("SCAN [exit *82] sending *00 / 0 / *99")
        await self._client.send_keypress("*00")
        await asyncio.sleep(STEP_DELAY)
        await self._client.send_keypress("0")
        await asyncio.sleep(STEP_DELAY)
        await self._client.send_keypress("*99")
        await asyncio.sleep(STEP_DELAY)

        for zone in ALL_ZONES:
            zt = zones[str(zone)].get("zone_type")
            if zt is not None and zt != 0:
                _LOGGER.info("Scanned zone %d: %r", zone, zones[str(zone)])
        return zones

    # ------------------------------------------------------------------
    # Custom word scanning (*82 custom word mode)
    # ------------------------------------------------------------------

    async def _scan_custom_words(self, code: str) -> dict[str, Any]:
        """
        Read custom words 01-12 via *82 custom word mode.

        Entry: *99{code}800*8211 → panel shows CUSTOM? 00 (start position).
        There is NO partition prompt — the second '1' answers YES to
        "Custom Words?" and the panel goes straight to word 00.

        For each word 01-12:
          Navigate: send 2-digit word number → "CUSTOM? NN [content]" [capture]
          Return:   send "8" → saves word unchanged, returns to CUSTOM? 00

        Exit: 00 → PROGRAM ALPHA? → 0 (no) → data-field → *99 → normal mode.

        See KEYSEQUENCES.md §10 for the confirmed sequence.
        """
        words: dict[str, Any] = {}

        # Entry: *99{code}800*8211 → CUSTOM? 00 (start position).
        # Send char-by-char so the panel can process each menu transition
        # (PROGRAM ALPHA? → Custom Words? → CUSTOM? 00) before the next
        # keystroke arrives.  Only the final '1' (YES to Custom Words?)
        # uses send_and_capture to confirm the panel has reached CUSTOM? 00.
        # This is setup only — not counted as a progress step.
        entry_keys = f"*99{code}800*8211"
        for ch in entry_keys[:-1]:
            await asyncio.sleep(CHAR_DELAY)
            await self._client.send_keypress(ch)
        await asyncio.sleep(STEP_DELAY)
        pre_display = await self._client.get_current_display()
        entry_display = await self._client.send_and_capture(
            entry_keys[-1], ignore_display=pre_display,
        )
        if entry_display is None:
            entry_display = await self._client.get_current_display() or ""
        _LOGGER.info("Custom word entry display: %r", entry_display)

        # Allow the REST state to synchronise with the WS-captured entry
        # display.  Without this, the very first _capture_with_fallback
        # call may snapshot a stale pre-entry display as its baseline,
        # causing the entry display ("CUSTOM? 00") to pass the _ignore
        # filter and be captured as word 01's result.
        await asyncio.sleep(STEP_DELAY)

        # Words 01-12: navigate by sending the 2-digit word number
        # (char-by-char via _capture_with_fallback), capture the display,
        # then press 8 to return to CUSTOM? 00.
        for wn in range(1, 13):
            digits = f"{wn:02d}"
            msg = f"Custom word {digits}: reading (*82 custom)"
            display = await self._capture_with_fallback(digits, msg)
            parsed = _parse_custom_word_display(display)
            words[digits] = {"word_num": wn, "content": parsed["content"],
                             "raw_display": display}
            # Press 8 to save (unchanged) and return to CUSTOM? 00
            await self._client.send_keypress("8")
            await asyncio.sleep(STEP_DELAY)

        # Exit custom word mode per §10.5:
        #   00  → exit to PROGRAM ALPHA?
        #   0   → no → data-field mode
        #   *99 → exit programming
        _LOGGER.info("SCAN [exit *82 custom words] sending 00 / 0 / *99")
        await self._client.send_keypress("00")
        await asyncio.sleep(STEP_DELAY)
        await self._client.send_keypress("0")
        await asyncio.sleep(STEP_DELAY)
        await self._client.send_keypress("*99")
        await asyncio.sleep(STEP_DELAY)

        for wn in range(1, 13):
            key = f"{wn:02d}"
            _LOGGER.info("Scanned custom word %s: %r", key, words.get(key))
        return words

    # ------------------------------------------------------------------
    # Reporting / dialer / pager field scanning
    # ------------------------------------------------------------------

    async def _scan_reporting(self, code: str) -> dict[str, Any]:
        """
        Read reporting, dialer, and pager fields (*40–*49, *160–*172).

        Uses #NN review mode for each field — read-only, no data accepted.
        Stays in programming mode for the entire batch: enters once, reads
        all fields with #NN, and exits with *99 at the end.

        For simple fields (1–3 values), `_wait_for_display_change` captures
        the single value display after the initial field-label display, then
        '#' dismisses the review back to the Field? prompt.
        For scrolling fields (phone numbers, account numbers), polls for
        multiple display updates and collects all digit pairs until the
        display goes blank or the panel auto-returns to Field?.
        """
        reporting: dict[str, Any] = {}

        # Enter programming mode once
        entry_display = await self._capture_with_fallback(
            f"*99{code}800", "Entering programming mode"
        )
        await self._wait_for_display_change(entry_display, max_wait=5.0)

        for field_num, key, label, scrolling, max_digits in REPORTING_FIELDS:
            display = await self._capture_with_fallback(
                f"#{field_num:03d}" if field_num >= 100 else f"#{field_num:02d}",
                f"Reading *{field_num} ({label})",
            )
            _LOGGER.debug("scan_reporting %s #%d display=%r", key, field_num, display)

            if scrolling:
                digits = await self._collect_scrolling_digits(
                    display, field_num, max_digits
                )
                reporting[key] = {
                    "field": field_num,
                    "label": label,
                    "value": digits,
                    "raw_display": display,
                }
                # Scrolling fields auto-return to "Field?" when done
            else:
                value_display = await self._wait_for_display_change(display, max_wait=15.0)
                # The panel first shows the field number on line 2, then
                # updates to the actual value ~1-2 s later.  If we caught
                # the field-number display (line2 == "48" for *48), wait
                # for one more display change to get the real value.
                val_line2 = _get_line2(value_display).strip()
                if val_line2 == str(field_num):
                    _LOGGER.debug(
                        "scan_reporting *%d: display still shows field number "
                        "(%r), waiting for value display", field_num, val_line2,
                    )
                    second = await self._wait_for_display_change(
                        value_display, max_wait=10.0,
                    )
                    if second != value_display:
                        value_display = second
                value = _parse_reporting_value(value_display, field_num)
                reporting[key] = {
                    "field": field_num,
                    "label": label,
                    "value": value,
                    "raw_display": value_display,
                }
                # Dismiss the review to get back to "Field?" prompt
                await self._client.send_keypress("#")
                await asyncio.sleep(STEP_DELAY)

            _LOGGER.info("Scanned *%d (%s) = %r", field_num, key,
                         reporting[key]["value"])

        # Exit programming mode
        await self._client.send_keypress("*99")
        await asyncio.sleep(STEP_DELAY)
        return reporting

    # ------------------------------------------------------------------
    # Keypad scanning
    # ------------------------------------------------------------------

    async def _scan_keypads(self, code: str) -> dict[str, Any]:
        """
        Read keypad configuration fields *190–*196 (keypads 2–8).

        Each field shows a single 2-digit combined value on line 2:
          - Tens digit = Partition/Enable: 0=disabled, 1=Part 1, 2=Part 2, 3=Common
          - Ones digit = Sound option: 0=all sounds, 1=suppress arm/E-E,
            2=suppress chime, 3=suppress all

        Panel sequence for #NNN review:
          1. Capture: "Keypad Addr.XX  |             NNN"  (field number)
          2. Display: "Keypad Addr.XX  |              10"  (combined value, e.g. Part 1 + All sounds)
          3. Panel may repeat/refresh the same display ~1 s later
          4. Returns to "Field?"

        Uses #NNN review mode — read-only, no data accepted.
        Stays in programming mode for the entire batch.
        """
        keypads: dict[str, Any] = {}

        # Keypad field *190 = keypad 2 (address 17), *196 = keypad 8 (address 23)
        KEYPAD_FIELDS = [
            (190, 2, 17),
            (191, 3, 18),
            (192, 4, 19),
            (193, 5, 20),
            (194, 6, 21),
            (195, 7, 22),
            (196, 8, 23),
        ]

        # Enter programming mode once
        entry_display = await self._capture_with_fallback(
            f"*99{code}800", "Entering programming mode"
        )
        await self._wait_for_display_change(entry_display, max_wait=5.0)

        for field_num, keypad_num, addr in KEYPAD_FIELDS:
            display = await self._capture_with_fallback(
                f"#{field_num:03d}",
                f"Reading keypad {keypad_num} (*{field_num})",
            )
            _LOGGER.debug(
                "scan_keypads keypad %d #%d initial=%r",
                keypad_num, field_num, display,
            )

            # The initial capture shows the field number on line 2.
            # Wait for the first sub-value (partition/enable).
            first_display = await self._wait_for_display_change(
                display, max_wait=10.0, poll_interval=0.3
            )
            first_line2 = _get_line2(first_display).strip()
            _LOGGER.debug(
                "scan_keypads keypad %d first_display=%r line2=%r",
                keypad_num, first_display, first_line2,
            )

            # The keypad field shows a single 2-digit combined value on
            # line 2: first digit = partition/enable, second digit = sound.
            # E.g. "10" = partition 1 + all sounds, "00" = disabled + all sounds.
            # The panel then repeats/refreshes the same display ~1 s later
            # before returning to "Field?".
            raw_val = _extract_raw_value(first_line2)
            if len(raw_val) >= 2:
                partition_enable = raw_val[-2]   # tens digit
                sound = raw_val[-1]              # ones digit
            else:
                partition_enable = raw_val
                sound = "0"

            key = str(keypad_num)
            keypads[key] = {
                "keypad": keypad_num,
                "address": addr,
                "field": field_num,
                "partition_enable": partition_enable,
                "sound": sound,
                "raw_combined": raw_val,
                "raw_display": display,
            }
            _LOGGER.info(
                "Scanned keypad %d (*%d): partition_enable=%s, sound=%s",
                keypad_num, field_num, partition_enable, sound,
            )

        # Exit programming mode
        await self._client.send_keypress("*99")
        await asyncio.sleep(STEP_DELAY)
        return keypads

    async def _collect_scrolling_digits(
        self, initial_display: str, field_num: int, max_digits: int,
        raw: bool = False,
    ) -> str:
        """
        Collect scrolling digit pairs from the panel display.

        After the initial field-label display, the panel scrolls through
        each value (~1 s per pair), showing a 2-digit pair on the right
        side of line 2. A blank line 2 (or return to "Field?") signals
        the end.

        *raw=False* (default): phone-number encoding — each 2-digit pair
        is decoded via ``_extract_digit_pair`` to a single character.
        Used for reporting fields (*43, *44, etc.).

        *raw=True*: single-digit scroll — the panel scrolls one digit at
        a time, shown as a 1-2 digit number on line 2 (e.g. "3", "0",
        "8", "0" for *34 exit delay = P1:30 P2:80).  Each display
        change yields ONE digit (the last character of the number).
        Consecutive identical digits (e.g. digit "0" followed by "0")
        are detected by a time-based cadence rather than display-change
        comparison, since the display text doesn't change between them.

        Uses fast REST polling (every 0.2 s) to detect display changes.
        The panel scrolls at ~1 s per digit, so polling at 0.2 s catches
        every transition.

        Returns the collected digit string, e.g. "12035049900" (decoded)
        or "3080" (raw, meaning P1=30, P2=80).
        """
        digits: list[str] = []
        POLL_INTERVAL = 0.2
        # Max total time to wait for all pairs to scroll through.
        MAX_SCROLL_TIME = max_digits * 3.0 + 5.0

        # --- Detect first content display via REST polling ---
        # The display transitions: header → (field-number) → pair 1 → …
        first = await self._wait_for_display_change(
            initial_display, max_wait=15.0, poll_interval=POLL_INTERVAL,
        )
        _LOGGER.debug(
            "_collect_scrolling_digits *%d: initial_display=%r  first_content=%r",
            field_num, initial_display, first,
        )
        if not first or first == initial_display:
            return ""  # No value stored — field is empty
        if "Field?" in first:
            return ""
        line2 = _get_line2(first)
        if not line2.strip():
            return ""

        # The panel first shows the field number on line 2 before
        # scrolling through digit pairs.  If we caught the field-number
        # display, wait for the real first digit pair.
        if line2.strip() == str(field_num):
            _LOGGER.debug(
                "_collect_scrolling_digits *%d: display shows field number "
                "(%r), waiting for first digit pair", field_num, line2.strip(),
            )
            first = await self._wait_for_display_change(
                first, max_wait=10.0, poll_interval=POLL_INTERVAL,
            )
            if not first or "Field?" in first:
                return ""
            line2 = _get_line2(first)
            if not line2.strip():
                return ""

        # Extract the first digit
        if raw:
            d = _extract_scrolling_digit(line2)
        else:
            d = _extract_digit_pair(line2)
        if d is None:
            return ""
        digits.append(d)
        _LOGGER.debug(
            "_collect_scrolling_digits *%d: pair[0] line2=%r => %r (raw=%s)",
            field_num, line2, d, raw,
        )

        # --- Collect remaining digits via fast REST polling ---
        # The panel shows each digit for ~1 s.  Consecutive identical
        # digits (e.g. "0" then "0", or "08" then "08") produce the
        # SAME display text.  We use a time-based cadence: if >=1.2 s
        # has passed since the last digit was captured and the display
        # hasn't changed to something new, we assume the panel repeated
        # the same digit.  Must be > 1.0 s (the actual scroll interval)
        # to avoid false positives on a single digit that hasn't scrolled
        # yet, but < 2.0 s to not miss two consecutive same-value digits.
        # This applies to both raw and non-raw modes.
        prev_display = first
        prev_capture_time = asyncio.get_event_loop().time()
        deadline = prev_capture_time + MAX_SCROLL_TIME
        collected = 1  # Already have digit[0]
        SAME_DIGIT_INTERVAL = 1.2  # Panel scrolls ~1 s per digit

        while collected < max_digits and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            current = await self._client.get_current_display()

            if not current:
                continue
            if "Field?" in current:
                _LOGGER.debug(
                    "_collect_scrolling_digits *%d: Field? detected — done",
                    field_num,
                )
                break  # Scrolling ended

            now = asyncio.get_event_loop().time()

            if current != prev_display:
                # Display changed — extract the new digit
                line2 = _get_line2(current)
                stripped = line2.strip()
                if not stripped:
                    break  # Blank = end of value

                if raw:
                    d = _extract_scrolling_digit(line2)
                else:
                    d = _extract_digit_pair(line2)
                if d is not None:
                    digits.append(d)
                    collected += 1
                    _LOGGER.debug(
                        "_collect_scrolling_digits *%d: pair[%d] line2=%r => %r",
                        field_num, collected - 1, line2, d,
                    )
                prev_display = current
                prev_capture_time = now

            elif (now - prev_capture_time) >= SAME_DIGIT_INTERVAL:
                # Same display but enough time has passed — the panel is
                # showing the same digit again (e.g. "08" followed by "08").
                line2 = _get_line2(current)
                if raw:
                    d = _extract_scrolling_digit(line2)
                else:
                    d = _extract_digit_pair(line2)
                if d is not None:
                    digits.append(d)
                    collected += 1
                    _LOGGER.debug(
                        "_collect_scrolling_digits *%d: pair[%d] (same-display repeat) "
                        "line2=%r => %r",
                        field_num, collected - 1, line2, d,
                    )
                prev_capture_time = now

        _LOGGER.debug("_collect_scrolling_digits *%d: collected %d pairs: %r",
                       field_num, len(digits), digits)
        return "".join(digits)

    # ------------------------------------------------------------------
    # Panel time scan (normal-mode command)
    # ------------------------------------------------------------------

    async def _scan_panel_time(self, user_code: str) -> dict[str, Any]:
        """
        Read the panel clock via View Time/Date: {user_code}#63.
        This is a normal-mode command (no programming mode needed).
        The panel displays the time for ~30 s then auto-exits.
        Returns dict: display, iso, skew_seconds, scan_epoch.
        skew_seconds is positive when the panel is behind wall-clock, negative when ahead.
        """
        # The preceding scan section (*scan_keypads) exits programming mode
        # with *99.  The panel needs a few seconds to fully return to its
        # normal idle display before it will accept the {user_code}#63
        # command.  If we send keys too soon the panel ignores them and
        # the capture just sees "****DISARMED**** Ready to Arm".
        await self._wait_for_normal_display(max_wait=10.0, poll_interval=0.5)

        display = await self._capture_with_fallback(
            f"{user_code}#63", "Reading panel time/date",
            expect_programming=False,
        )
        _LOGGER.info("SCAN [panel time] => %r", display)
        return _parse_panel_time_display(display)

    async def read_panel_time(self, user_code: str) -> dict[str, Any]:
        """
        Standalone panel time read (outside of a full scan).
        Sends {user_code}#63 and captures the resulting display.
        After capturing, waits for the panel to return to its normal
        display (~30 s) so the caller can safely release control.
        Returns dict: display, iso, skew_seconds, scan_epoch.
        """
        if self._scanning:
            raise RuntimeError("Scan in progress")
        keys = f"{user_code}#63"
        # Send all chars except last individually, then capture on last char
        for ch in keys[:-1]:
            await asyncio.sleep(CHAR_DELAY)
            await self._client.send_keypress(ch)
        await asyncio.sleep(STEP_DELAY)
        pre_display = await self._client.get_current_display()
        display = await self._client.send_and_capture(
            keys[-1], ignore_display=pre_display,
        )
        if display is None:
            # Fallback: check if display updated during the delay
            display = await self._client.get_current_display() or ""
        _LOGGER.info("read_panel_time => %r", display)
        result = _parse_panel_time_display(display)

        # The panel shows the time for ~30 s before auto-returning to
        # normal mode.  Wait here so the UI stays locked and the user
        # cannot issue conflicting commands while the panel is busy.
        await self._wait_for_normal_display(max_wait=35.0)

        return result

    async def _wait_for_normal_display(
        self, max_wait: float = 35.0, poll_interval: float = 1.0
    ) -> None:
        """
        Poll get_current_display until the panel leaves a transient screen
        (e.g. the Time/Date view) and returns to its normal idle display.

        Normal idle displays contain patterns like "Ready to Arm",
        "DISARMED", "ARMED", "NOT READY", "FAULT", etc.
        The Time/Date screen contains "Time/Date" and a time pattern.
        """
        _TIME_PATTERN = re.compile(
            r"Time/Date|\d{2}:\d{2}(?:AM|PM)", re.IGNORECASE
        )
        _NORMAL_PATTERN = re.compile(
            r"Ready|DISARMED|ARMED|NOT READY|FAULT|ALARM|Hit \*",
            re.IGNORECASE,
        )
        deadline = asyncio.get_event_loop().time() + max_wait
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(poll_interval)
            current = await self._client.get_current_display() or ""
            if _NORMAL_PATTERN.search(current) and not _TIME_PATTERN.search(current):
                _LOGGER.debug(
                    "_wait_for_normal_display: panel returned to normal => %r",
                    current,
                )
                return
        _LOGGER.debug(
            "_wait_for_normal_display: timed out after %.0fs", max_wait
        )

    # ------------------------------------------------------------------
    # Cache persistence
    # ------------------------------------------------------------------

    async def _save_cache(self, data: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        try:
            with open(CACHE_PATH, "w") as f:
                json.dump(data, f, indent=2)
            _LOGGER.info("Config cache saved to %s", CACHE_PATH)
        except Exception as exc:
            _LOGGER.warning("Failed to save cache: %s", exc)

    async def _flush_log(self) -> None:
        """Write the current scan log to /data/scan_log.json."""
        try:
            with open(SCAN_LOG_PATH, "w") as f:
                json.dump(self._scan_log, f, indent=2)
        except Exception as exc:
            _LOGGER.warning("Failed to save scan log: %s", exc)

    def get_scan_log(self) -> list[dict]:
        """Return the in-memory scan log for the most recent scan."""
        return list(self._scan_log)


# ---------------------------------------------------------------------------
# Display text parsers
# ---------------------------------------------------------------------------

# Vista 20P delay special encoding (official programming guide K5305-1PRV5):
#   *34 exit delay:   97 = 120 s  (98/99 are NOT valid for *34)
#   *35 entry delay 1: 97 = 120 s, 98 = 180 s, 99 = 240 s
#   *36 entry delay 2: same as *35
_DELAY_DECODE: dict[int, int] = {97: 120, 98: 180, 99: 240}
# Encode: 180 and 240 are only valid for entry delays (*35/*36), NOT exit (*34).
# Server validates this per-field; encode is shared for all three.
_DELAY_ENCODE: dict[int, int] = {120: 97, 180: 98, 240: 99}


def delay_decode(raw: int) -> int:
    """Convert panel raw delay value to seconds."""
    return _DELAY_DECODE.get(raw, raw)


def delay_encode(seconds: int) -> int:
    """Convert seconds to panel raw delay value."""
    return _DELAY_ENCODE.get(seconds, seconds)


def _parse_acct_display(text: str) -> str | None:
    """
    Parse an account number field display — returns the digit string as-is,
    preserving leading zeros.  Used for *43/*44/*45/*46 account number fields.
    """
    nums = re.findall(r"[0-9A-Fa-f]+", text)
    if not nums:
        return None
    return nums[-1]


def _parse_fire_timeout_display(text: str) -> int | None:
    """
    Parse field *32 (Fire Alarm Sounder Timeout).
    The panel shows a text label rather than a bare number:
      value 1 (no timeout): display = "No Fire Timeout"  (no trailing digit)
      value 0 (timeout):    display probably contains a "0" as the last digit
    Strategy: look for a trailing digit first; if none, infer from label text.
    """
    nums = re.findall(r"\d+", text)
    if nums:
        return int(nums[-1])  # raw value 0 or 1
    # No digit found — panel is showing the label for value=1 ("No Fire Timeout")
    lower = text.lower()
    if "no" in lower or "timeout" in lower:
        return 1
    return None


def _parse_delay_display(text: str) -> int | None:
    """
    Parse a delay field display. The panel shows something like:
      "*34  60"  or  "EXIT DELAY  60"  or just the value "60"
    Returns seconds (decoded from the panel's raw value).
    *34 special: raw 97 = 120 s only.
    *35/*36 specials: raw 97 = 120 s, 98 = 180 s, 99 = 240 s.
    """
    nums = re.findall(r"\d+", text)
    if not nums:
        return None
    return delay_decode(int(nums[-1]))


def _parse_zone_type_display(text: str) -> dict[str, Any]:
    """
    Parse *58 expert mode zone summary line.
    Format: "Zn ZT P RC HW:RT"  e.g. "01 03 1 10 EL:1"
    or two-line: line1="Zn ZT P RC HW:RT", line2="01 03 1 10 EL:1"

    Returns dict with: zone_type(int), partition(int), report_code(int),
    hw_type(int), response_time(int), input_type(int).

    The 2-digit input_type stored by the panel encodes both hw_type and
    response_time: input_type = hw_type * 10 + response_time.
    The display renders this as "HW:RT" (e.g. "EL:1" = EOL, 350 ms).
    """
    result: dict[str, Any] = {
        "zone_type": None,
        "partition": 1,
        "report_code": 1,
        "hw_type": 0,
        "response_time": 1,
        "input_type": 1,
    }
    # The *58 expert-mode display is a 32-char string laid out as two 16-char
    # LCD lines joined into one string:
    #   Line 1 header: "Zn ZT P RC HW:RT"  (16 chars — ends in "RT")
    #   Line 2 data  : "02 03 1 10 EL:1 "  (16 chars)
    # Together: "Zn ZT P RC HW:RT02 03 1 10 EL:1"
    #
    # The two lines may be separated by \n (split-display clients) or joined
    # directly (single-string clients).  Search anywhere in the text for the
    # "RT" sentinel that marks the boundary, followed by the data fields.
    m = re.search(
        r"RT\s*(\d{2})\s+(\d{2})\s+(\d)\s+(\d+)\s+([A-Z]+):(\d)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if m:
        result["zone_type"] = int(m.group(2))
        result["partition"] = int(m.group(3))
        result["report_code"] = int(m.group(4))
        hw_raw = m.group(5).upper()
        rt_raw = int(m.group(6))
        hw_id_map = {"EL": 0, "EOL": 0, "NC": 1, "NO": 2, "ZD": 3, "DB": 4}
        hw_id = hw_id_map.get(hw_raw, 0)
        result["hw_type"] = hw_id
        result["response_time"] = rt_raw
        result["input_type"] = hw_id * 10 + rt_raw
    return result


def _parse_zone_name_display(text: str) -> str:
    """
    Parse zone descriptor display from *82.
    The EVL alpha field is 32 chars split as two 16-char LCD lines joined by '\n'.
    A two-word name like "FRONT DOOR" may span both lines, so join them first
    before stripping the '* Zn NN' header prefix.
    """
    # Join both LCD lines into one string, collapsing the newline to a space.
    full = re.sub(r"\n", " ", text)
    # Strip leading '* Zn NN' or 'Zn NN' prefix (case-insensitive).
    stripped = re.sub(r"^\*?\s*Zn\s+\d+\s*", "", full, flags=re.IGNORECASE).strip()
    # Collapse any internal runs of whitespace left by the join.
    return re.sub(r"\s{2,}", " ", stripped).strip()


def _parse_custom_word_display(text: str) -> dict[str, Any]:
    """
    Parse custom word display from *82 custom word mode.
    Display format: "CUSTOM? NN [content]"
    The content is up to 10 characters, possibly blank or space-padded.
    """
    full = re.sub(r"\n", " ", text)
    m = re.match(r"CUSTOM\?\s*(\d+)\s*(.*)", full, re.IGNORECASE)
    if m:
        return {"word_num": int(m.group(1)), "content": m.group(2).strip()}
    return {"word_num": None, "content": full.strip()}


def _parse_panel_time_display(text: str) -> dict[str, Any]:
    """
    Parse the View Time/Date display returned by {user_code}#63.
    Expected display (32-char alpha split as 16×2 LCD lines):
      Line 1: "Time/Date    SUN"
      Line 2: "01:15PM 03/29/26"
    Returns dict: display(str), iso(str|None), skew_seconds(float|None), scan_epoch(float).
    skew_seconds is positive when the panel is behind wall-clock, negative when ahead.
    """
    result: dict[str, Any] = {
        "display": text,
        "iso": None,
        "skew_seconds": None,
        "scan_epoch": time.time(),
    }
    m = re.search(
        r"(\d{2}):(\d{2})(AM|PM)\s+(\d{2})/(\d{2})/(\d{2})",
        text,
        re.IGNORECASE,
    )
    if not m:
        _LOGGER.warning("Could not parse panel time from display: %r", text)
        return result
    hour = int(m.group(1))
    minute = int(m.group(2))
    ampm = m.group(3).upper()
    month = int(m.group(4))
    day = int(m.group(5))
    year = 2000 + int(m.group(6))
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    try:
        panel_dt = datetime.datetime(year, month, day, hour, minute)
        now = datetime.datetime.now().replace(second=0, microsecond=0)
        result["iso"] = panel_dt.isoformat()
        result["skew_seconds"] = (now - panel_dt).total_seconds()
    except (ValueError, OverflowError) as exc:
        _LOGGER.warning("Panel time value error: %s", exc)
    return result


# ---------------------------------------------------------------------------
# Reporting field parsers
# ---------------------------------------------------------------------------

def _get_line2(text: str) -> str:
    """
    Extract line 2 of a 32-char keypad display.
    If the text contains a newline, return the second half;
    otherwise return chars 16–32.
    """
    if "\n" in text:
        parts = text.split("\n", 1)
        return parts[1] if len(parts) > 1 else ""
    return text[16:] if len(text) > 16 else ""


def _extract_scrolling_digit(line2: str) -> str | None:
    """
    Extract a single scrolling digit from line 2 of a keypad display.

    For delay fields (*34, *35, *36), the panel scrolls one digit at a
    time.  The TPI user_zone_field shows the digit zero-padded (e.g. "03"
    for digit 3, "08" for digit 8, "00" for digit 0).  The HA sensor
    displays this as the rightmost number on line 2.

    Returns the single digit character (last char of the number), or None
    if no number is found.
    """
    stripped = line2.strip()
    if not stripped:
        return None
    nums = re.findall(r"\d+", stripped)
    if not nums:
        return None
    # The actual digit is the last character of the number
    return nums[-1][-1]


def _extract_digit_pair(line2: str) -> str | None:
    """
    Extract a digit pair from the right side of line 2.
    The panel shows a 2-digit value right-aligned on line 2.
    Returns the 1-digit decoded value, or None if no digit found.

    Panel display encoding for phone/account fields:
    Shows the raw digit entered at this position (00-15).
    For standard digits 0-9, the value IS the digit.
    For special values: 10=0(#10), 11=*(#11), 12=#(#12), 13=pause(#13),
    14=E(#14), 15=F(#15).
    """
    stripped = line2.strip()
    if not stripped:
        return None
    # Find the last number on the line
    nums = re.findall(r"\d+", stripped)
    if not nums:
        return None
    raw = int(nums[-1])
    # Decode: 0-9 are literal, 10-15 are specials
    if 0 <= raw <= 9:
        return str(raw)
    decode_map = {10: "0", 11: "*", 12: "#", 13: "P", 14: "E", 15: "F"}
    return decode_map.get(raw, str(raw))


def _extract_raw_value(line2: str) -> str:
    """
    Extract a raw 2-digit value from line 2 of a keypad display.

    Unlike _extract_digit_pair() (which decodes phone-number encoding),
    this returns the number as a zero-padded 2-digit string exactly as
    displayed.  E.g. "01" → "01", "00" → "00", "03" → "03".

    Returns "00" if no number is found.
    """
    stripped = line2.strip()
    if not stripped:
        return "00"
    nums = re.findall(r"\d+", stripped)
    if not nums:
        return "00"
    return nums[-1].zfill(2)


def _parse_reporting_value(text: str, field_num: int) -> str | None:
    """
    Parse a simple (non-scrolling) reporting field display.
    The panel shows the field label on line 1 and value on line 2,
    e.g. "Phone Sys        01" where 01 = the stored value.
    Returns the value string, or None if empty.
    """
    line2 = _get_line2(text)
    stripped = line2.strip()
    if not stripped:
        return None
    # Extract all digit groups from line 2
    nums = re.findall(r"\d+", stripped)
    if not nums:
        return None
    # For fields that show the field number first, skip it
    # The value is typically the last number shown after the field number is gone
    # (since we called _wait_for_display_change to skip past the field number)
    return stripped


