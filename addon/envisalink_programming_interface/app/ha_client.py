"""
HA WebSocket + REST client.

Connects to the HA Supervisor proxy on startup, authenticates with
SUPERVISOR_TOKEN, subscribes to keypad sensor state changes, and
broadcasts updates to all connected frontend WebSocket clients.

Also provides send_keypress() for fire-and-forget key delivery and
send_and_capture() for the scanner (waits for the panel display to
update before returning the new display text).
"""

import asyncio
import json
import logging
import os
from typing import Callable

import aiohttp

_LOGGER = logging.getLogger(__name__)

HA_BASE = "http://supervisor/core/api"
HA_WS = "ws://supervisor/core/websocket"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

# How long (seconds) to wait for a panel display update after a keypress.
# The Vista 20P can take up to 10+ seconds to respond in some states
# (e.g. entering programming mode, heavy bus activity).  Use a generous
# timeout so the fallback path is only hit on genuine no-response situations.
CAPTURE_TIMEOUT = 15.0


class HAClient:
    def __init__(self, keypad_sensor: str, partition_entity: str) -> None:
        self._keypad_sensor = keypad_sensor
        self._partition_entity = partition_entity
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._broadcast_callbacks: list[Callable[[dict], None]] = []
        # One-shot futures used by send_and_capture()
        self._capture_futures: list[asyncio.Future] = []
        self._connected = False
        self._msg_id = 0
        # Persists any config validation error so it can be replayed to
        # browsers that connect after the initial validation runs.
        self._config_error: str | None = None
        # Expose the default capture timeout as an instance attribute so the
        # scanner can reference it in timeout warning messages.
        self.CAPTURE_TIMEOUT = CAPTURE_TIMEOUT

    @property
    def config_error(self) -> str | None:
        return self._config_error

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
        )
        # Validate entities exist before starting the WS loop
        asyncio.create_task(self._validate_and_start())

    async def _validate_and_start(self) -> None:
        await asyncio.sleep(1)  # give HA a moment after startup
        for entity_id, label in (
            (self._keypad_sensor, "keypad_sensor"),
            (self._partition_entity, "partition_entity"),
        ):
            try:
                async with self._session.get(
                    f"{HA_BASE}/states/{entity_id}"
                ) as resp:
                    if resp.status == 404:
                        _LOGGER.error(
                            "Entity '%s' (%s) not found in Home Assistant. "
                            "Check the add-on Configuration tab and set the correct entity ID.",
                            entity_id,
                            label,
                        )
                        error_msg = (
                            f"Entity not found: {entity_id!r} ({label}). "
                            "Open the add-on Configuration tab and correct the entity ID, "
                            "then restart the add-on."
                        )
                        self._config_error = error_msg
                        for cb in list(self._broadcast_callbacks):
                            try:
                                cb(
                                    {
                                        "type": "ha_error",
                                        "error": error_msg,
                                    }
                                )
                            except Exception:
                                pass
            except Exception as exc:
                _LOGGER.warning("Could not validate entity %s: %s", entity_id, exc)
        asyncio.create_task(self._ws_loop())

    async def stop(self) -> None:
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()

    # ------------------------------------------------------------------
    # WebSocket loop
    # ------------------------------------------------------------------

    async def _ws_loop(self) -> None:
        retry_delay = 5
        while True:
            try:
                async with self._session.ws_connect(HA_WS) as ws:
                    self._ws = ws
                    _LOGGER.info("Connected to HA WebSocket")
                    await self._ws_handshake(ws)
                    retry_delay = 5
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_ws_message(json.loads(msg.data))
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
            except Exception as exc:
                _LOGGER.warning("HA WS error: %s — retrying in %ds", exc, retry_delay)
            finally:
                self._connected = False
                self._ws = None
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)

    async def _ws_handshake(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        # Expect auth_required
        msg = json.loads((await ws.receive()).data)
        assert msg["type"] == "auth_required"
        # Send auth
        await ws.send_json({"type": "auth", "access_token": SUPERVISOR_TOKEN})
        # Expect auth_ok
        msg = json.loads((await ws.receive()).data)
        if msg["type"] != "auth_ok":
            raise RuntimeError(f"HA WS auth failed: {msg}")
        _LOGGER.info("HA WS authenticated")
        self._connected = True
        # Subscribe to state_changed trigger on keypad sensor
        self._msg_id += 1
        await ws.send_json(
            {
                "id": self._msg_id,
                "type": "subscribe_trigger",
                "trigger": {
                    "platform": "state",
                    "entity_id": self._keypad_sensor,
                },
            }
        )

    async def _handle_ws_message(self, msg: dict) -> None:
        if msg.get("type") != "event":
            return
        trigger = msg.get("event", {}).get("variables", {}).get("trigger", {})
        to_state = trigger.get("to_state", {})
        if not to_state:
            # Fallback: check event.data.new_state (subscribe_events format)
            to_state = (
                msg.get("event", {}).get("data", {}).get("new_state", {})
            )
        if not to_state:
            return

        display = to_state.get("state", "")
        attributes = to_state.get("attributes", {})
        payload = {"display": display, "attributes": attributes}

        # --- Diagnostic: log every WS display event ---
        n_pending = sum(1 for f in self._capture_futures if not f.done())
        _LOGGER.debug(
            "WS EVENT display=%r  pending_futures=%d",
            display, n_pending,
        )

        # Resolve any pending capture futures.
        # Futures may carry an `_ignore_display` attribute — either a string
        # (exact-match) or a callable predicate ``(str) -> bool``.  Displays
        # that match / satisfy the predicate are skipped, preventing stale
        # or intermediate events (e.g. "Field?" transits during #NN field
        # navigation) from resolving the future prematurely.
        still_pending = []
        for fut in list(self._capture_futures):
            if fut.done():
                continue
            ignore = getattr(fut, "_ignore_display", None)
            if ignore is not None:
                skip = ignore(display) if callable(ignore) else (display == ignore)
                if skip:
                    _LOGGER.debug(
                        "WS EVENT  → SKIPPED by predicate (display=%r)",
                        display,
                    )
                    still_pending.append(fut)
                    continue
            _LOGGER.debug(
                "WS EVENT  → RESOLVED future with display=%r",
                display,
            )
            fut.set_result(display)
        self._capture_futures = still_pending

        # Broadcast to all frontend WebSocket listeners
        for cb in list(self._broadcast_callbacks):
            try:
                cb(payload)
            except Exception as exc:
                _LOGGER.debug("Broadcast callback error: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_broadcast_callback(self, cb: Callable[[dict], None]) -> None:
        self._broadcast_callbacks.append(cb)

    def remove_broadcast_callback(self, cb: Callable[[dict], None]) -> None:
        self._broadcast_callbacks.discard(cb) if hasattr(
            self._broadcast_callbacks, "discard"
        ) else self._broadcast_callbacks.remove(cb) if cb in self._broadcast_callbacks else None

    @property
    def connected(self) -> bool:
        return self._connected

    async def get_zone_bypass_states(self, zone_nums: list[int]) -> dict[int, bool | None]:
        """Return bypass state for each zone number.  True=bypassed, False=not, None=entity absent.

        Reads the `bypassed` attribute from the zone binary sensor entities
        (binary_sensor.envisalink_new_zone_N or similar).  Works on all panel
        types including Honeywell/Vista.  The switch-based approach only works
        on DSC/Uno panels where bypass switches are enabled.
        """
        result: dict[int, bool | None] = {z: None for z in zone_nums}
        try:
            async with self._session.get(f"{HA_BASE}/states") as resp:
                if resp.status != 200:
                    return result
                all_states: list[dict] = await resp.json()
        except Exception as exc:
            _LOGGER.warning("get_zone_bypass_states: failed to fetch states: %s", exc)
            return result
        for state in all_states:
            entity_id = state.get("entity_id", "")
            if not entity_id.startswith("binary_sensor."):
                continue
            attrs = state.get("attributes", {})
            zone_num = attrs.get("zone")
            if zone_num is None:
                continue
            try:
                z = int(zone_num)
            except (TypeError, ValueError):
                continue
            if z in result:
                bypassed = attrs.get("bypassed")
                if bypassed is not None:
                    result[z] = bool(bypassed)
        return result

    async def get_current_display(self) -> str:
        """Return the current keypad sensor state via REST."""
        try:
            async with self._session.get(
                f"{HA_BASE}/states/{self._keypad_sensor}"
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("state", "")
        except Exception as exc:
            _LOGGER.warning("Failed to fetch keypad state: %s", exc)
        return ""

    async def send_keypress(self, keys: str) -> bool:
        """Send a keypress string to the partition entity via alarm_keypress service."""
        try:
            async with self._session.post(
                f"{HA_BASE}/services/envisalink_new/alarm_keypress",
                json={
                    "entity_id": self._partition_entity,
                    "keypress": keys,
                },
            ) as resp:
                if resp.status in (200, 201):
                    return True
                text = await resp.text()
                _LOGGER.warning("alarm_keypress failed %d: %s", resp.status, text)
                return False
        except Exception as exc:
            _LOGGER.warning("send_keypress error: %s", exc)
            return False

    async def send_and_capture(
        self, keys: str, timeout: float = CAPTURE_TIMEOUT,
        ignore_display: str | Callable[[str], bool] | None = None,
    ) -> str | None:
        """
        Send a keypress string and wait for the next panel display update.
        Returns the new display text, or None on timeout.

        *ignore_display* may be a **string** (exact-match) or a **callable**
        predicate ``(str) -> bool``.  Displays that match / satisfy the
        predicate are silently skipped, preventing stale or intermediate
        events from resolving the future prematurely.
        """
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        if ignore_display is not None:
            fut._ignore_display = ignore_display  # type: ignore[attr-defined]
        self._capture_futures.append(fut)
        _LOGGER.debug(
            "send_and_capture: keys=%r  ignore=%s  total_futures=%d",
            keys,
            "predicate" if callable(ignore_display) else repr(ignore_display),
            len(self._capture_futures),
        )
        try:
            ok = await self.send_keypress(keys)
            if not ok:
                if fut in self._capture_futures:
                    self._capture_futures.remove(fut)
                return None
            result = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
            _LOGGER.debug(
                "send_and_capture: keys=%r  RESOLVED => %r", keys, result,
            )
            return result
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "send_and_capture: keys=%r  TIMED OUT after %.1fs", keys, timeout,
            )
            if fut in self._capture_futures:
                self._capture_futures.remove(fut)
            return None

    async def wait_for_next_update(self, timeout: float = CAPTURE_TIMEOUT) -> str | None:
        """Wait for the next panel display update without sending any keys.

        Registers a capture future and waits for the next state_changed event
        to resolve it.  Returns the display text, or None on timeout.
        Useful for passively capturing scrolling displays (phone numbers, etc.).
        """
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._capture_futures.append(fut)
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except asyncio.TimeoutError:
            if fut in self._capture_futures:
                self._capture_futures.remove(fut)
            return None

    async def discover_entities(self) -> dict[str, list[str]]:
        """Query HA for candidate keypad_sensor and partition_entity IDs.

        Scores entities by how closely their ID / friendly name matches
        the envisalink integration naming conventions.  Returns up to
        three candidates per slot, highest-score first.
        """
        keypad_candidates: list[tuple[int, str]] = []
        partition_candidates: list[tuple[int, str]] = []
        try:
            async with self._session.get(f"{HA_BASE}/states") as resp:
                if resp.status != 200:
                    return {"keypad_sensor": [], "partition_entity": []}
                all_states: list[dict] = await resp.json()
        except Exception as exc:
            _LOGGER.warning("discover_entities: failed to fetch states: %s", exc)
            return {"keypad_sensor": [], "partition_entity": []}

        for state in all_states:
            eid = state.get("entity_id", "")
            name = state.get("attributes", {}).get("friendly_name", "").lower()
            eid_l = eid.lower()

            if eid.startswith("sensor."):
                score = 0
                if "envisalink" in eid_l and "keypad" in eid_l:
                    score = 4
                elif "envisalink" in eid_l:
                    score = 3
                elif "keypad" in eid_l or "keypad" in name:
                    score = 2
                elif "alarm" in eid_l or "partition" in eid_l or "alarm" in name or "partition" in name:
                    score = 1
                if score:
                    keypad_candidates.append((score, eid))

            elif eid.startswith("alarm_control_panel."):
                # All alarm_control_panel entities are candidates; score by specificity
                if "envisalink" in eid_l and "partition" in eid_l:
                    score = 4
                elif "envisalink" in eid_l:
                    score = 3
                elif "partition" in eid_l or "partition" in name:
                    score = 2
                else:
                    score = 1  # any alarm_control_panel is a valid candidate
                partition_candidates.append((score, eid))

        keypad_candidates.sort(key=lambda x: x[0], reverse=True)
        partition_candidates.sort(key=lambda x: x[0], reverse=True)
        return {
            "keypad_sensor": [eid for _, eid in keypad_candidates[:3]],
            "partition_entity": [eid for _, eid in partition_candidates[:3]],
        }
