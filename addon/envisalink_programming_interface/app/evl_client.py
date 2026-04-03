"""Direct TCP connection to the EVL TPI — Honeywell protocol.

Provides exactly the same public interface as HAClient so server.py can swap
between the two without touching the scanner or route handlers.

Important: the EVL allows only ONE TCP client at a time.  Before enabling
direct mode the envisalink_new integration must be disabled in Home Assistant
(Settings → Devices & Services → disable), otherwise the EVL will
force-close one of the two sessions.

Authentication:
  The EVL sends the ASCII prompt ``Login:`` on connect.  We reply with the
  TPI password followed by CRLF.  The EVL responds with ``OK`` (success) or
  ``FAILED`` (wrong password) or ``Timed Out!`` (no reply in time).

Keypresses:
  Each character is sent as a separate ``^03,{partition},{char}$`` TPI command
  with _CHAR_DELAY seconds between them so the EVL's command buffer doesn't
  overflow.

Display updates:
  The EVL broadcasts ``%00,partition,flags,user_zone,beep[,alpha]$`` keypad
  updates.  We extract the alpha (display text) field and forward it to
  registered callbacks, matching the HAClient broadcast format exactly.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Callable

_LOGGER = logging.getLogger(__name__)

_LOGIN_TIMEOUT = 15.0    # seconds to complete the Login:/OK handshake
_CHAR_DELAY    = 0.35    # seconds between successive keypress commands
_KEEPALIVE_INT = 30      # seconds between keepalive pings (^00,$)
_RECONNECT_MIN = 10      # starting reconnect backoff (seconds)
_RECONNECT_MAX = 120     # backoff cap (seconds)

# How long to wait for the panel to respond to a keypress sequence.
# The Vista 20P can take up to ~16 s to update the display after entering
# a data-field review command (#NN) while under bus load.  Use a generous
# timeout so the fallback abort path is only hit on genuine no-response.
CAPTURE_TIMEOUT = 25.0


class EvlClient:
    """Direct Honeywell EVL TPI client — no Home Assistant dependency."""

    def __init__(self, host: str, port: int, password: str, partition: int = 1) -> None:
        self._host = host
        self._port = port
        self._password = password
        self._partition = partition
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._shutdown = False
        self._recv_buffer = b""
        self._current_display = ""
        self._current_attributes: dict = {}
        self._broadcast_callbacks: list[Callable[[dict], None]] = []
        self._capture_futures: list[asyncio.Future] = []
        self._config_error: str | None = None
        self._reconnect_delay = _RECONNECT_MIN
        # Expose the default capture timeout as an instance attribute so the
        # scanner can reference it in timeout warning messages.
        self.CAPTURE_TIMEOUT = CAPTURE_TIMEOUT

    # ------------------------------------------------------------------
    # Properties — identical to HAClient interface
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def config_error(self) -> str | None:
        return self._config_error

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._shutdown = False
        asyncio.create_task(self._connection_loop(), name="evl_direct_conn")

    async def stop(self) -> None:
        self._shutdown = True
        await self._close_connection()

    async def _close_connection(self) -> None:
        self._connected = False
        self._recv_buffer = b""
        if self._writer:
            try:
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), 3.0)
            except Exception:
                pass
            finally:
                self._writer = None
        self._reader = None

    # ------------------------------------------------------------------
    # Connection loop — reconnects with exponential backoff
    # ------------------------------------------------------------------

    async def _connection_loop(self) -> None:
        while not self._shutdown:
            try:
                _LOGGER.info("EVL direct: connecting to %s:%d", self._host, self._port)
                coro = asyncio.open_connection(self._host, self._port)
                self._reader, self._writer = await asyncio.wait_for(coro, _LOGIN_TIMEOUT)
                await self._run_session()
                self._reconnect_delay = _RECONNECT_MIN  # clean session — reset backoff
            except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
                _LOGGER.warning("EVL direct: connection error: %s", exc)
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "EVL direct: timed out connecting to %s:%d", self._host, self._port
                )
            except Exception as exc:
                _LOGGER.warning("EVL direct: unexpected error: %s", exc)
            finally:
                await self._close_connection()
                self._broadcast({"type": "evl_status", "connected": False})

            if not self._shutdown:
                _LOGGER.info("EVL direct: reconnecting in %ds", self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, _RECONNECT_MAX)

    # ------------------------------------------------------------------
    # Session — login handshake then read loop
    # ------------------------------------------------------------------

    async def _run_session(self) -> None:
        """Run the Honeywell TPI login handshake; on OK start the read loop."""
        deadline = asyncio.get_event_loop().time() + _LOGIN_TIMEOUT
        logged_in = False

        while not logged_in:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError("EVL login timeout")

            chunk = await asyncio.wait_for(self._reader.read(1024), remaining)
            if not chunk:
                raise ConnectionError("EVL closed connection during login")

            self._recv_buffer += chunk

            # Login messages are \n-terminated; process all complete lines
            while b"\n" in self._recv_buffer:
                nl = self._recv_buffer.index(b"\n")
                raw = self._recv_buffer[: nl + 1]
                self._recv_buffer = self._recv_buffer[nl + 1 :]
                try:
                    line = raw.decode("ascii").strip()
                except UnicodeDecodeError:
                    continue

                _LOGGER.debug("EVL login << %r", line)

                if line == "Login:":
                    self._writer.write((self._password + "\r\n").encode("ascii"))
                    await self._writer.drain()

                elif line == "OK":
                    _LOGGER.info("EVL direct: logged in to %s:%d", self._host, self._port)
                    self._connected = True
                    self._config_error = None
                    self._reconnect_delay = _RECONNECT_MIN
                    self._broadcast({"type": "evl_status", "connected": True})
                    logged_in = True

                elif line in ("FAILED", "Timed Out!"):
                    msg = (
                        "EVL rejected the password. Check the password in Connection Settings."
                        if line == "FAILED"
                        else "EVL login timed out — no Login: prompt received."
                    )
                    self._config_error = msg
                    _LOGGER.error("EVL direct: %s", msg)
                    self._broadcast({"type": "ha_error", "error": msg})
                    return  # don't reconnect immediately for auth failures

        if not logged_in:
            return

        # Start keepalive task and main read loop
        ka_task = asyncio.create_task(self._keepalive_loop(), name="evl_keepalive")
        try:
            await self._read_loop()
        finally:
            ka_task.cancel()
            try:
                await ka_task
            except asyncio.CancelledError:
                pass

    async def _read_loop(self) -> None:
        """Receive TPI messages until the connection is closed."""
        while not self._shutdown and self._reader:
            try:
                chunk = await asyncio.wait_for(self._reader.read(4096), 35.0)
            except asyncio.TimeoutError:
                # EVL silent for 35 s — prod it with a keepalive
                await self._send_raw("^00,$")
                continue
            except (ConnectionResetError, OSError, BrokenPipeError) as exc:
                _LOGGER.warning("EVL direct: connection dropped: %s", exc)
                break

            if not chunk:
                _LOGGER.info("EVL direct: server closed connection")
                break

            self._recv_buffer += chunk
            self._flush_buffer()

    def _flush_buffer(self) -> None:
        """Extract and dispatch all complete TPI messages from _recv_buffer."""
        while self._recv_buffer:
            dollar = self._recv_buffer.find(b"$")
            newline = self._recv_buffer.find(b"\n")

            if dollar == -1 and newline == -1:
                break  # no complete message yet

            if dollar != -1 and (newline == -1 or dollar < newline):
                # '$'-terminated TPI message (Honeywell events and command acks)
                raw = self._recv_buffer[: dollar + 1]
                self._recv_buffer = self._recv_buffer[dollar + 1 :]
            else:
                # '\n'-terminated (login messages already handled; DSC messages)
                raw = self._recv_buffer[: newline + 1]
                self._recv_buffer = self._recv_buffer[newline + 1 :]

            try:
                msg = raw.decode("ascii").strip()
            except UnicodeDecodeError:
                _LOGGER.debug("EVL direct: discarding non-ASCII frame")
                continue

            if not msg:
                continue

            self._dispatch(msg)

    def _dispatch(self, msg: str) -> None:
        """Parse one TPI message and update display state."""
        _LOGGER.debug("EVL direct << %s", msg)

        # Bug 14 pattern: bare %XX notification fused with ^YY ack
        # e.g. "%02^00,00$" — split at ^ and dispatch each part separately.
        first_comma = msg.find(",")
        caret = msg.find("^")
        if msg.startswith("%") and caret != -1 and (first_comma == -1 or caret < first_comma):
            self._dispatch(msg[:caret] + "$")
            self._dispatch(msg[caret:])
            return

        # Bug 15: two %-prefixed messages fused without a $ separator
        # (e.g. rapid keypad updates after exiting programming mode).
        # The second %XX code embeds in the alpha field of the first,
        # producing raw TPI data on the display.
        pct = re.search(r'(?<=.)%[0-9A-Fa-f]{2},', msg)
        if pct:
            self._dispatch(msg[:pct.start()] + "$")
            self._dispatch(msg[pct.start():])
            return

        # We only need %00 keypad updates for the display; skip everything else.
        if not msg.startswith("%00"):
            return

        # Parse: "%00,partition,flags,user_zone,beep[,alpha]$"
        core = msg.rstrip("$")                   # strip trailing $
        parts = core.split(",")                  # ["%00", part, flags, uz, beep, ...]
        data_fields = parts[1:]                  # [partition, flags, user_zone, beep, alpha...]

        # Recombine alpha — it may contain commas (zone names with punctuation)
        if len(data_fields) > 5:
            data_fields[4] = ",".join(data_fields[4:])
            del data_fields[5:]

        if len(data_fields) < 4:
            return  # malformed / too few fields

        alpha = data_fields[4].strip() if len(data_fields) >= 5 else ""

        # Safety net: Vista panels have a 32-char keypad display (16×2).
        # Alpha exceeding this indicates residual message corruption (Bug 15).
        if len(alpha) > 32:
            _LOGGER.debug("EVL direct: alpha too long (%d), truncating: %r", len(alpha), alpha)
            alpha = alpha[:32]

        self._current_display = alpha

        # Parse the 4-char hex flags into LED attribute booleans.
        # Names match the HA integration (honeywell_client.py) so updateLeds() works.
        attributes = {}
        try:
            flags = int(data_fields[1], 16)
            attributes = {
                "alarm": bool(flags & 0x0001),
                "alarm_in_memory": bool(flags & 0x0002),
                "armed_away": bool(flags & 0x0004),
                "ac_present": bool(flags & 0x0008),
                "armed_bypass": bool(flags & 0x0010),
                "chime": bool(flags & 0x0020),
                "armed_zero_entry_delay": bool(flags & 0x0080),
                "alarm_fire_zone": bool(flags & 0x0100),
                "trouble": bool(flags & 0x0200),
                "ready": bool(flags & 0x1000),
                "fire": bool(flags & 0x2000),
                "low_battery": bool(flags & 0x4000),
                "armed_stay": bool(flags & 0x8000),
            }
        except (ValueError, IndexError):
            _LOGGER.debug("EVL direct: unable to parse flags '%s'", data_fields[1])
        self._current_attributes = attributes

        payload = {"display": alpha, "attributes": attributes}

        # Resolve any pending send_and_capture futures.
        # Futures may carry an `_ignore_display` attribute — either a string
        # (exact-match) or a callable predicate ``(str) -> bool``.  Displays
        # that match / satisfy the predicate are skipped.
        if self._capture_futures:
            _LOGGER.debug("EVL direct: resolving %d capture future(s) with %r",
                          len(self._capture_futures), alpha)
        still_pending = []
        for fut in list(self._capture_futures):
            if fut.done():
                continue
            ignore = getattr(fut, "_ignore_display", None)
            if ignore is not None:
                skip = ignore(alpha) if callable(ignore) else (alpha == ignore)
                if skip:
                    still_pending.append(fut)
                    continue
            fut.set_result(alpha)
        self._capture_futures = still_pending

        # Broadcast display update to all WebSocket listeners
        self._broadcast({"type": "display", **payload})

    async def _keepalive_loop(self) -> None:
        """Send a keepalive command every _KEEPALIVE_INT seconds."""
        while not self._shutdown and self._connected:
            await asyncio.sleep(_KEEPALIVE_INT)
            if self._connected:
                _LOGGER.debug("EVL direct: sending keepalive")
                await self._send_raw("^00,$")

    async def _send_raw(self, data: str) -> None:
        if self._writer:
            try:
                self._writer.write((data + "\r\n").encode("ascii"))
                await self._writer.drain()
                _LOGGER.debug("EVL >> %s", data)
            except Exception as exc:
                _LOGGER.debug("EVL direct: send error: %s", exc)

    def _broadcast(self, payload: dict) -> None:
        for cb in list(self._broadcast_callbacks):
            try:
                cb(payload)
            except Exception as exc:
                _LOGGER.debug("EVL direct: broadcast callback error: %s", exc)

    # ------------------------------------------------------------------
    # Public API — matches HAClient interface exactly
    # ------------------------------------------------------------------

    def add_broadcast_callback(self, cb: Callable[[dict], None]) -> None:
        self._broadcast_callbacks.append(cb)

    def remove_broadcast_callback(self, cb: Callable[[dict], None]) -> None:
        if cb in self._broadcast_callbacks:
            self._broadcast_callbacks.remove(cb)

    async def get_current_display(self) -> str:
        return self._current_display

    async def get_current_attributes(self) -> dict:
        return self._current_attributes

    async def send_keypress(self, keys: str) -> bool:
        """Send each character in *keys* as a separate ^03 TPI command.

        Uses _CHAR_DELAY between characters so the EVL's command buffer
        doesn't overflow (mirrors pyenvisalink's CHAR_DELAY = 0.35 s).
        """
        if not self._connected or not self._writer:
            _LOGGER.warning("EVL direct: send_keypress called but not connected")
            return False
        try:
            for ch in keys:
                await self._send_raw(f"^03,{self._partition},{ch}$")
                await asyncio.sleep(_CHAR_DELAY)
            return True
        except Exception as exc:
            _LOGGER.warning("EVL direct: send_keypress error: %s", exc)
            return False

    async def send_and_capture(
        self, keys: str, timeout: float = CAPTURE_TIMEOUT,
        ignore_display: str | Callable[[str], bool] | None = None,
    ) -> str | None:
        """Send *keys* then wait for the next %00 keypad display update.

        The capture future is registered BEFORE send_keypress so that
        display updates arriving during the inter-character delay are
        captured immediately.  Since _capture_with_fallback always calls
        this with a single character, there are no intermediate per-key
        updates to worry about.

        *ignore_display* may be a string (exact-match) or a callable
        predicate ``(str) -> bool``.  Matching displays are skipped.
        """
        if not self._connected:
            return None

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        if ignore_display is not None:
            fut._ignore_display = ignore_display  # type: ignore[attr-defined]
        self._capture_futures.append(fut)

        ok = await self.send_keypress(keys)
        if not ok:
            if fut in self._capture_futures:
                self._capture_futures.remove(fut)
            return None

        _LOGGER.debug("EVL direct: waiting up to %.0fs for display update (keys=%r)", timeout, keys)
        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
            elapsed = time.monotonic() - t0
            _LOGGER.debug("EVL direct: display captured in %.2fs: %r", elapsed, result)
            return result
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - t0
            _LOGGER.debug(
                "EVL direct: send_and_capture timed out after %.1fs for keys=%r",
                elapsed, keys,
            )
            if fut in self._capture_futures:
                self._capture_futures.remove(fut)
            return None

    async def wait_for_next_update(self, timeout: float = CAPTURE_TIMEOUT) -> str | None:
        """Wait for the next %00 display update without sending any keys.

        Registers a capture future and waits for the next keypad update
        event to resolve it.  Returns the display text, or None on timeout.
        Useful for passively capturing scrolling displays (phone numbers, etc.).
        """
        if not self._connected:
            return None
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._capture_futures.append(fut)
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except asyncio.TimeoutError:
            if fut in self._capture_futures:
                self._capture_futures.remove(fut)
            return None
