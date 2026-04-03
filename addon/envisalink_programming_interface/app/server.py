"""
FastAPI server — main application entry point.

Routes:
  GET  /                         — serve index.html
  GET  /static/*                 — static assets
  WS   /ws                       — real-time panel display pushes to browser
  POST /api/code                 — set/clear installer+user code (stored in /data/code.json)
  GET  /api/code/set             — returns {set: bool} (never returns the code itself)
  GET  /api/user_code_hint       — returns saved user code hint for pre-filling modal
  GET  /api/state                — current keypad sensor display text
  POST /api/keypress             — raw passthrough (virtual keypad)
  POST /api/scan                 — start full config scan (SSE progress via WS)
  GET  /api/config               — return cached config (or 404 if no cache)
  POST /api/configure            — apply a single setting change
  POST /api/eventlog             — fetch N event log entries
  GET  /api/vocab                — vocabulary list for frontend word picker
  GET  /api/zone_types           — zone type list for frontend dropdowns
  GET  /api/zone_states          — live bypass state for zones 1–48 from HA entities
  GET  /api/suggest_entities     — ranked candidate entity IDs for keypad_sensor / partition_entity
  POST /api/apply_entities       — save corrected entity IDs and reinitialise HA client in-process
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiofiles
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from .evl_client import EvlClient
from .ha_client import HAClient
from .panel_commands import (
    REPORTING_BY_KEY,
    REPORTING_DELETABLE_FIELDS,
    REVERSE_VOCAB,
    VOCAB,
    ZONE_TYPES,
    ZONE_TYPE_DESCS,
    build_custom_word,
    build_installer_code_change,
    build_keypad_config,
    build_master_code_change,
    build_reporting_field_delete,
    build_reporting_field_set,
    build_set_field,
    build_set_time,
    build_user_authority,
    build_user_code_set,
    build_user_delete,
    build_user_partition,
    build_zone_name,
    build_zone_type_edit,
)
from .scanner import PanelScanner, ScanAbortError

_LOGGER = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

STATIC_DIR = Path(__file__).parent / "static"
CODE_PATH = "/data/code.json"
ENTITIES_PATH = "/data/entities.json"
SCAN_LOG_PATH = "/data/scan_log.json"
EVL_CONFIG_PATH = "/data/evl_direct.json"
EVENTLOG_CAPTURE_TIMEOUT = 30.0  # max seconds to wait capturing N log entries


# ---------------------------------------------------------------------------
# Entity persistence (/data/entities.json)
# ---------------------------------------------------------------------------

DEFAULT_PARTITION_ENTITY = "alarm_control_panel.envisalink_new_partition_1"
DEFAULT_KEYPAD_SENSOR = "sensor.envisalink_new_keypad_partition_1"


# ---------------------------------------------------------------------------
# EVL direct-connection config (/data/evl_direct.json)
# ---------------------------------------------------------------------------

def _load_evl_config() -> dict:
    """Return EVL direct connection config, or defaults (HA mode)."""
    if os.path.exists(EVL_CONFIG_PATH):
        try:
            with open(EVL_CONFIG_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"mode": "ha", "evl_host": "", "evl_port": 4025, "evl_password": ""}


def _save_evl_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(EVL_CONFIG_PATH), exist_ok=True)
    with open(EVL_CONFIG_PATH, "w") as f:
        json.dump(cfg, f)


# ---------------------------------------------------------------------------
# Entity persistence (/data/entities.json)
# ---------------------------------------------------------------------------

def _load_entities() -> dict[str, str] | None:
    """Return saved entity IDs, or None if the file doesn't exist yet."""
    if os.path.exists(ENTITIES_PATH):
        try:
            with open(ENTITIES_PATH) as f:
                data = json.load(f)
            if data.get("keypad_sensor") and data.get("partition_entity"):
                return data
        except Exception:
            pass
    return None


def _save_entities(keypad_sensor: str, partition_entity: str) -> None:
    with open(ENTITIES_PATH, "w") as f:
        json.dump({"keypad_sensor": keypad_sensor, "partition_entity": partition_entity}, f)


# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------

ha_client: HAClient | None = None
scanner: PanelScanner | None = None
# Active WebSocket connections from browsers
_ws_clients: set[WebSocket] = set()
# Temporary list of keypad updates for event log capture
_log_capture_queue: asyncio.Queue | None = None
_log_capture_count: int = 0


async def _resolve_entities() -> tuple[str, str]:
    """Return (keypad_sensor, partition_entity) to use.

    Priority:
    1. /data/entities.json  — previously saved (including user corrections)
    2. Auto-discovery via HA REST API, saved to /data/entities.json
    3. Hard-coded defaults (discovery failed / no envisalink entities found)
    """
    saved = _load_entities()
    if saved:
        _LOGGER.info(
            "Loaded entities from storage: keypad=%s partition=%s",
            saved["keypad_sensor"], saved["partition_entity"],
        )
        return saved["keypad_sensor"], saved["partition_entity"]

    # First boot — run discovery using a temporary session
    _LOGGER.info("No saved entities — running auto-discovery")
    import aiohttp
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
    try:
        async with aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {supervisor_token}"}
        ) as session:
            async with session.get("http://supervisor/core/api/states") as resp:
                if resp.status == 200:
                    all_states = await resp.json()
                    ks, pe = _score_entities(all_states)
                    if ks and pe:
                        _save_entities(ks, pe)
                        _LOGGER.info("Auto-detected entities: keypad=%s partition=%s", ks, pe)
                        return ks, pe
    except Exception as exc:
        _LOGGER.warning("Entity auto-discovery failed: %s", exc)

    _LOGGER.warning("Using default entity IDs (auto-discovery found nothing)")
    return DEFAULT_KEYPAD_SENSOR, DEFAULT_PARTITION_ENTITY


def _score_entities(all_states: list[dict]) -> tuple[str, str]:
    """Pick the best keypad_sensor and partition_entity from a list of HA states."""
    keypad_candidates: list[tuple[int, str]] = []
    partition_candidates: list[tuple[int, str]] = []
    for state in all_states:
        eid = state.get("entity_id", "")
        name = state.get("attributes", {}).get("friendly_name", "").lower()
        eid_l = eid.lower()
        if eid.startswith("sensor."):
            if "envisalink" in eid_l and "keypad" in eid_l:
                score = 4
            elif "envisalink" in eid_l:
                score = 3
            elif "keypad" in eid_l or "keypad" in name:
                score = 2
            elif "alarm" in eid_l or "partition" in eid_l or "alarm" in name or "partition" in name:
                score = 1
            else:
                score = 0
            if score:
                keypad_candidates.append((score, eid))
        elif eid.startswith("alarm_control_panel."):
            if "envisalink" in eid_l and "partition" in eid_l:
                score = 4
            elif "envisalink" in eid_l:
                score = 3
            elif "partition" in eid_l or "partition" in name:
                score = 2
            else:
                score = 1
            partition_candidates.append((score, eid))
    keypad_candidates.sort(key=lambda x: x[0], reverse=True)
    partition_candidates.sort(key=lambda x: x[0], reverse=True)
    ks = keypad_candidates[0][1] if keypad_candidates else ""
    pe = partition_candidates[0][1] if partition_candidates else ""
    return ks, pe


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ha_client, scanner
    evl_cfg = _load_evl_config()
    if evl_cfg.get("mode") == "direct" and evl_cfg.get("evl_host") and evl_cfg.get("evl_password"):
        client: HAClient | EvlClient = EvlClient(
            host=evl_cfg["evl_host"],
            port=int(evl_cfg.get("evl_port", 4025)),
            password=evl_cfg["evl_password"],
        )
    else:
        keypad_sensor, partition_entity = await _resolve_entities()
        client = HAClient(keypad_sensor, partition_entity)
    ha_client = client
    scanner = PanelScanner(ha_client)
    ha_client.add_broadcast_callback(_on_display_update)
    await ha_client.start()
    yield
    await ha_client.stop()


app = FastAPI(lifespan=lifespan)


class _NormalizePathMiddleware(BaseHTTPMiddleware):
    """Collapse leading double-slashes that HA ingress can introduce."""
    async def dispatch(self, request: Request, call_next):
        scope = request.scope
        path = scope.get("path", "/")
        if path.startswith("//"):
            scope["path"] = "/" + path.lstrip("/")
        return await call_next(request)


app.add_middleware(_NormalizePathMiddleware)

# Serve static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Display broadcast
# ---------------------------------------------------------------------------

def _on_display_update(payload: dict) -> None:
    """Called by HAClient or EvlClient on every display update or status event."""
    global _log_capture_queue, _log_capture_count
    # ha_error and evl_status are forwarded as-is (not display content)
    if payload.get("type") in ("ha_error", "evl_status"):
        asyncio.create_task(_broadcast_ws(payload))
        return
    # Broadcast to all WS clients (fire & forget)
    asyncio.create_task(_broadcast_ws({"type": "display", **payload}))
    # If an event log capture is in progress, feed it
    if _log_capture_queue is not None and _log_capture_count > 0:
        try:
            _log_capture_queue.put_nowait(payload.get("display", ""))
            _log_capture_count -= 1
        except asyncio.QueueFull:
            pass


async def _broadcast_ws(data: dict) -> None:
    dead: list[WebSocket] = []
    for ws in list(_ws_clients):
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.discard(ws)


def _scrub_keys(keys: str) -> str:
    """Replace installer / user codes in a keypress string with '****' for sniffer display."""
    code = _load_code()
    user_code = getattr(app.state, "session_user_code", None) or _load_user_code()
    scrubbed = keys
    if code:
        scrubbed = scrubbed.replace(code, "****")
    if user_code and user_code != code:
        scrubbed = scrubbed.replace(user_code, "****")
    return scrubbed


async def _broadcast_keypress(keys: str, source: str) -> None:
    """Broadcast a keypress_sent event to all WS clients (for sniffer capture).

    source: short label like 'keypad', 'configure', 'recovery', 'eventlog'.
    Codes are scrubbed before broadcast.
    """
    scrubbed = _scrub_keys(keys)
    await _broadcast_ws({"type": "keypress_sent", "keys": scrubbed, "source": source})


# ---------------------------------------------------------------------------
# Installer code helpers
# ---------------------------------------------------------------------------

def _load_code() -> str | None:
    if os.path.exists(CODE_PATH):
        try:
            with open(CODE_PATH) as f:
                return json.load(f).get("code")
        except Exception:
            pass
    return None


def _load_user_code() -> str | None:
    if os.path.exists(CODE_PATH):
        try:
            with open(CODE_PATH) as f:
                return json.load(f).get("user_code") or None
        except Exception:
            pass
    return None


def _save_code(code: str, user_code: str = "") -> None:
    os.makedirs(os.path.dirname(CODE_PATH), exist_ok=True)
    with open(CODE_PATH, "w") as f:
        json.dump({"code": code, "user_code": user_code}, f)


def _clear_code() -> None:
    if os.path.exists(CODE_PATH):
        os.remove(CODE_PATH)


def _require_code() -> str:
    code = _load_code()
    if not code:
        raise HTTPException(status_code=401, detail="Installer code not set")
    return code


# ---------------------------------------------------------------------------
# Base path helper (ingress)
# ---------------------------------------------------------------------------

def _base_path(request: Request) -> str:
    return request.headers.get("X-Ingress-Path", "")


# ---------------------------------------------------------------------------
# Routes — root
# ---------------------------------------------------------------------------

@app.get("/")
async def root(request: Request):
    # Inject base path into index.html dynamically
    base = _base_path(request)
    html_path = STATIC_DIR / "index.html"
    async with aiofiles.open(html_path, mode="r") as f:
        html = await f.read()
    html = html.replace("__BASE_PATH__", base)
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    # Send current state immediately on connect
    if ha_client:
        display = await ha_client.get_current_display()
        attributes = {}
        if hasattr(ha_client, "get_current_attributes"):
            attributes = await ha_client.get_current_attributes()
        await ws.send_json({
            "type": "display",
            "display": display,
            "attributes": attributes,
            "connected": ha_client.connected,
        })
    await ws.send_json({"type": "code_status", "set": _load_code() is not None})
    if ha_client and ha_client.config_error:
        await ws.send_json({"type": "ha_error", "error": ha_client.config_error})
    try:
        while True:
            # Keep connection alive; browser sends pings as {"type":"ping"}
            data = await ws.receive_json()
            if data.get("type") == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)


# ---------------------------------------------------------------------------
# Routes — installer code
# ---------------------------------------------------------------------------

class CodeRequest(BaseModel):
    code: str
    user_code: str = ""
    remember: bool = True


@app.post("/api/code")
async def set_code(req: CodeRequest):
    if not req.code.isdigit() or len(req.code) < 4:
        raise HTTPException(status_code=400, detail="Code must be at least 4 digits")
    if req.user_code and (not req.user_code.isdigit() or len(req.user_code) < 4):
        raise HTTPException(status_code=400, detail="User code must be at least 4 digits")
    if req.remember:
        _save_code(req.code, req.user_code)
    else:
        # Store in memory only (don't persist)
        app.state.session_code = req.code
        app.state.session_user_code = req.user_code
    await _broadcast_ws({"type": "code_status", "set": True})
    return {"ok": True}


@app.get("/api/code/set")
async def code_is_set():
    code = _load_code() or getattr(app.state, "session_code", None)
    return {"set": code is not None}


@app.delete("/api/code")
async def clear_code():
    _clear_code()
    app.state.session_code = None
    app.state.session_user_code = None
    await _broadcast_ws({"type": "code_status", "set": False})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Routes — entity discovery / auto-configuration
# ---------------------------------------------------------------------------

@app.get("/api/connection")
async def get_connection():
    """Return the current connection mode and EVL direct config (password never returned)."""
    cfg = _load_evl_config()
    saved = _load_entities()
    return {
        "mode": cfg.get("mode", "ha"),
        "evl_host": cfg.get("evl_host", ""),
        "evl_port": cfg.get("evl_port", 4025),
        "evl_password_set": bool(cfg.get("evl_password")),
        "connected": bool(ha_client and ha_client.connected),
        "keypad_sensor": saved["keypad_sensor"] if saved else DEFAULT_KEYPAD_SENSOR,
        "partition_entity": saved["partition_entity"] if saved else DEFAULT_PARTITION_ENTITY,
    }


class ConnectionRequest(BaseModel):
    mode: str              # "ha" or "direct"
    evl_host: str = ""
    evl_port: int = 4025
    evl_password: str = ""  # empty string means keep the existing saved password


@app.post("/api/connection")
async def set_connection(req: ConnectionRequest):
    """Save connection settings and reinitialise the active client in-process."""
    if req.mode not in ("ha", "direct"):
        raise HTTPException(status_code=400, detail="mode must be 'ha' or 'direct'")
    if req.mode == "direct":
        if not req.evl_host:
            raise HTTPException(status_code=400, detail="evl_host is required for direct mode")
        if not (1 <= req.evl_port <= 65535):
            raise HTTPException(status_code=400, detail="evl_port must be 1–65535")
    # Preserve existing password if caller sent an empty string (UI "keep saved")
    existing = _load_evl_config()
    password = req.evl_password if req.evl_password else existing.get("evl_password", "")
    new_cfg = {
        "mode": req.mode,
        "evl_host": req.evl_host,
        "evl_port": req.evl_port,
        "evl_password": password,
    }
    _save_evl_config(new_cfg)
    asyncio.create_task(_reinit_client(new_cfg))
    return {"ok": True}


@app.get("/api/suggest_entities")
async def suggest_entities():
    """Return likely entity IDs for keypad_sensor and partition_entity.

    Not available in direct mode (no HA connection is used).
    """
    if not ha_client or not hasattr(ha_client, "discover_entities"):
        return {"keypad_sensor": [], "partition_entity": []}
    return await ha_client.discover_entities()


class ApplyEntitiesRequest(BaseModel):
    keypad_sensor: str
    partition_entity: str


@app.post("/api/apply_entities")
async def apply_entities(req: ApplyEntitiesRequest):
    """Save corrected entity IDs and reinitialise the HA client in-process.

    No restart required — the running HAClient is stopped, replaced with a
    new one using the corrected IDs, and the scanner is updated to match.
    """
    global ha_client, scanner
    if not ha_client or not hasattr(ha_client, "discover_entities"):
        raise HTTPException(status_code=400, detail="apply_entities is only available in HA mode")
    if not req.keypad_sensor.startswith("sensor."):
        raise HTTPException(status_code=400, detail="keypad_sensor must be a sensor.* entity")
    if not req.partition_entity.startswith("alarm_control_panel."):
        raise HTTPException(
            status_code=400, detail="partition_entity must be an alarm_control_panel.* entity"
        )
    # Persist first — if reinit fails the values are still saved for next boot
    try:
        _save_entities(req.keypad_sensor, req.partition_entity)
    except Exception as exc:
        _LOGGER.error("_save_entities failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    # Reinit in-process
    asyncio.create_task(_reinit_ha_client(req.keypad_sensor, req.partition_entity))
    return {"ok": True}


async def _reinit_ha_client(keypad_sensor: str, partition_entity: str) -> None:
    """Stop the current client and start a fresh HAClient with new entity IDs."""
    global ha_client, scanner
    old = ha_client
    if old:
        old.remove_broadcast_callback(_on_display_update)
        await old.stop()
    new_client = HAClient(keypad_sensor, partition_entity)
    scanner = PanelScanner(new_client)
    new_client.add_broadcast_callback(_on_display_update)
    ha_client = new_client
    await new_client.start()
    _LOGGER.info("HAClient reinitialised: keypad=%s partition=%s", keypad_sensor, partition_entity)


async def _reinit_client(cfg: dict) -> None:
    """Stop the current client and start the appropriate new one based on cfg."""
    global ha_client, scanner
    old = ha_client
    if old:
        old.remove_broadcast_callback(_on_display_update)
        await old.stop()
    if cfg.get("mode") == "direct" and cfg.get("evl_host") and cfg.get("evl_password"):
        new_client: HAClient | EvlClient = EvlClient(
            host=cfg["evl_host"],
            port=int(cfg.get("evl_port", 4025)),
            password=cfg["evl_password"],
        )
        _LOGGER.info("Reinitialised as EvlClient: %s:%d", cfg["evl_host"], cfg.get("evl_port", 4025))
    else:
        keypad_sensor, partition_entity = await _resolve_entities()
        new_client = HAClient(keypad_sensor, partition_entity)
        _LOGGER.info("Reinitialised as HAClient: keypad=%s", keypad_sensor)
    scanner = PanelScanner(new_client)
    new_client.add_broadcast_callback(_on_display_update)
    ha_client = new_client
    await new_client.start()


# ---------------------------------------------------------------------------
# Routes — user code hint
# ---------------------------------------------------------------------------

@app.get("/api/user_code_hint")
async def get_user_code_hint():
    """Return user code for pre-filling the code modal.

    Returns locally saved code from /data/code.json, or "" if not saved yet.
    """
    return {"code": _load_user_code() or ""}


# ---------------------------------------------------------------------------
# Routes — panel state
# ---------------------------------------------------------------------------

@app.get("/api/state")
async def get_state():
    if not ha_client:
        raise HTTPException(status_code=503, detail="Client not ready")
    display = await ha_client.get_current_display()
    return {"display": display, "connected": ha_client.connected}


# ---------------------------------------------------------------------------
# Routes — raw keypress (virtual keypad)
# ---------------------------------------------------------------------------

class KeypressRequest(BaseModel):
    keys: str


class ScanSectionRequest(BaseModel):
    section: str


@app.post("/api/keypress")
async def send_keypress(req: KeypressRequest):
    if not ha_client:
        raise HTTPException(status_code=503, detail="Client not ready")
    ok = await ha_client.send_keypress(req.keys)
    if ok:
        await _broadcast_keypress(req.keys, "keypad")
    return {"ok": ok}


# ---------------------------------------------------------------------------
# Routes — config scan
# ---------------------------------------------------------------------------

@app.post("/api/scan")
async def start_scan():
    if not ha_client or not scanner:
        raise HTTPException(status_code=503, detail="Client not ready")
    if scanner.scanning:
        raise HTTPException(status_code=409, detail="Scan already in progress")
    code = _require_code()
    user_code = getattr(app.state, "session_user_code", None) or _load_user_code() or ""
    await _broadcast_ws({"type": "scan_started"})

    async def _progress_cb(step: int, total: int, msg: str) -> None:
        await _broadcast_ws({"type": "scan_progress", "step": step, "total": total, "msg": msg})

    try:
        result = await scanner.scan_all(code, user_code=user_code, on_progress=_progress_cb)
        await _broadcast_ws({"type": "scan_complete", "config": result})
        return result
    except ScanAbortError as exc:
        _LOGGER.error("Scan aborted (panel unresponsive): %s", exc)
        await _broadcast_ws({"type": "scan_error", "error": str(exc), "aborted": True})
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        _LOGGER.error("Scan failed: %s", exc)
        await _broadcast_ws({"type": "scan_error", "error": str(exc), "aborted": False})
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/scan_section")
async def start_scan_section(req: ScanSectionRequest):
    """Partial scan — re-read just one section ('zones' or 'system')."""
    if not ha_client or not scanner:
        raise HTTPException(status_code=503, detail="Client not ready")
    if scanner.scanning:
        raise HTTPException(status_code=409, detail="Scan already in progress")
    section = req.section
    if section not in ("zones", "words", "system", "reporting", "keypads"):
        raise HTTPException(status_code=400, detail=f"Unknown section: {section!r}")
    code = _require_code()
    await _broadcast_ws({"type": "scan_started", "section": section})

    async def _progress_cb(step: int, total: int, msg: str) -> None:
        await _broadcast_ws({"type": "scan_progress", "step": step, "total": total, "msg": msg})

    try:
        result = await scanner.scan_section(code, section, on_progress=_progress_cb)
        await _broadcast_ws({"type": "scan_complete", "config": result, "section": section})
        return result
    except ScanAbortError as exc:
        _LOGGER.error("Section scan aborted (%s): %s", section, exc)
        await _broadcast_ws({"type": "scan_error", "error": str(exc), "aborted": True})
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        _LOGGER.error("Section scan failed (%s): %s", section, exc)
        await _broadcast_ws({"type": "scan_error", "error": str(exc), "aborted": False})
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/config")
async def get_config():
    if not scanner:
        raise HTTPException(status_code=503, detail="Client not ready")
    config = await scanner.load_cache()
    if config is None:
        raise HTTPException(status_code=404, detail="No config cache")
    return config


@app.post("/api/panel_time")
async def read_panel_time():
    """Read the panel clock on demand ({user_code}#63). Requires user code."""
    if not scanner:
        raise HTTPException(status_code=503, detail="Client not ready")
    user_code = (
        getattr(app.state, "session_user_code", None)
        or _load_user_code()
        or ""
    )
    if not user_code:
        raise HTTPException(status_code=400, detail="User/master code required")
    try:
        result = await scanner.read_panel_time(user_code)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return result


@app.get("/api/scan_log")
async def get_scan_log():
    """Return the debug log entries from the most recent scan.

    Each entry: {t, level, step, keys, display, note}.
    Also reads /data/scan_log.json if the in-memory log is empty (e.g. after restart).
    """
    if scanner and scanner.get_scan_log():
        return {"entries": scanner.get_scan_log()}
    # Fallback: read from persisted file
    if os.path.exists(SCAN_LOG_PATH):
        try:
            with open(SCAN_LOG_PATH) as f:
                entries = json.load(f)
            return {"entries": entries}
        except Exception:
            pass
    return {"entries": []}


# ---------------------------------------------------------------------------
# Keypad stepwise programming helper
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Routes — apply a setting change
# ---------------------------------------------------------------------------

class ConfigureRequest(BaseModel):
    field: str          # e.g. "exit_delay", "entry_delay_1", "zone", "zone_name", "custom_word", "time"
    value: Any = None   # field-specific value
    zone_num: int | None = None
    # zone fields:
    zone_type: int | None = None
    partition: int | None = None
    report_code: int | None = None
    input_type: int | None = None
    # zone name fields:
    word_ids: list[int] | None = None
    # custom word fields:
    word_num: int | None = None
    text: str | None = None
    # reporting fields:
    reporting_key: str | None = None  # key from REPORTING_BY_KEY
    # keypad fields:
    keypad_num: int | None = None         # 2-8
    partition_enable: int | None = None   # 00=disabled, 01-03=partition
    sound: int | None = None              # 00-03


@app.post("/api/configure")
async def configure(req: ConfigureRequest):
    if not ha_client:
        raise HTTPException(status_code=503, detail="Client not ready")
    code = _require_code()
    cmd: str | None = None
    already_sent = False

    if req.field == "time":
        import datetime
        cmd = build_set_time(code, datetime.datetime.now())

    elif req.field in ("fire_timeout", "bell_timeout"):
        field_map_opt = {"fire_timeout": 32, "bell_timeout": 33}
        field_num = field_map_opt[req.field]
        raw_val = int(req.value)
        if req.field == "fire_timeout" and raw_val not in (0, 1):
            raise HTTPException(status_code=400, detail="Fire timeout: valid values are 0 or 1")
        if req.field == "bell_timeout" and raw_val not in range(5):
            raise HTTPException(status_code=400, detail="Bell timeout: valid values are 0–4")
        cmd = build_set_field(code, field_num, f"{raw_val:02d}")

    elif req.field in ("exit_delay", "entry_delay_1", "entry_delay_2"):
        field_map = {"exit_delay": 34, "entry_delay_1": 35, "entry_delay_2": 36}
        field_num = field_map[req.field]
        raw_str = str(req.value).strip()

        if len(raw_str) == 4 and raw_str.isdigit():
            # Dual-partition raw string, e.g. "6060" (Part 1 raw + Part 2 raw)
            p1 = int(raw_str[:2])
            p2 = int(raw_str[2:])
            if req.field == "exit_delay":
                for p in (p1, p2):
                    if p > 96 and p != 97:
                        raise HTTPException(status_code=400, detail="Exit delay raw: valid values are 0–96 or 97 (120 s)")
            else:
                for p in (p1, p2):
                    if p > 96 and p not in (97, 98, 99):
                        raise HTTPException(status_code=400, detail="Entry delay raw: valid values are 0–96 or 97/98/99")
            cmd = build_set_field(code, field_num, raw_str)
        else:
            # Legacy: single seconds value
            seconds = int(raw_str)
            if req.field == "exit_delay" and seconds > 96 and seconds != 120:
                raise HTTPException(status_code=400, detail="Exit delay: valid values are 0–96 s or 120 s")
            if req.field != "exit_delay" and seconds > 96 and seconds not in (120, 180, 240):
                raise HTTPException(status_code=400, detail="Entry delay: valid values are 0–96 s or 120, 180, 240 s")
            from .scanner import delay_encode
            raw_val = delay_encode(seconds)
            cmd = build_set_field(code, field_num, f"{raw_val:02d}")

    elif req.field == "zone":
        if req.zone_num is None:
            raise HTTPException(status_code=400, detail="zone_num required")
        from .scanner import HARDWIRED_ZONES
        partition = req.partition if req.partition is not None else 1
        # Hardwired zones (1-8) have input_type; expansion/wireless (9+) do not
        it = (req.input_type if req.input_type is not None else 1) if req.zone_num in HARDWIRED_ZONES else None
        cmd = build_zone_type_edit(
            code,
            req.zone_num,
            req.zone_type or 0,
            partition,
            req.report_code if req.report_code is not None else 1,
            it,
        )

    elif req.field == "zone_name":
        if req.zone_num is None:
            raise HTTPException(status_code=400, detail="zone_num required")
        cmd = build_zone_name(code, req.zone_num, req.word_ids or [])

    elif req.field == "custom_word":
        if req.word_num is None or not 1 <= req.word_num <= 12:
            raise HTTPException(status_code=400, detail="word_num required (1-12)")
        text = (req.text or "").strip()[:10]
        if text and not re.fullmatch(r"[A-Za-z0-9 ]+", text):
            raise HTTPException(status_code=400, detail="Only A-Z, 0-9, and space are allowed")
        cmd = build_custom_word(code, req.word_num, text)

    elif req.field == "reporting":
        rk = req.reporting_key
        if not rk or rk not in REPORTING_BY_KEY:
            raise HTTPException(status_code=400, detail=f"Unknown reporting_key: {rk!r}")
        meta = REPORTING_BY_KEY[rk]
        field_num = meta[0]
        val = str(req.value or "").strip()
        if not val:
            raise HTTPException(status_code=400, detail="value required for reporting field")
        # Allow digits plus special encoded chars (#NN sequences are pre-expanded by caller)
        if not re.fullmatch(r"[0-9#*]+", val):
            raise HTTPException(status_code=400, detail="value must contain only digits and #/* for special chars")
        cmd = build_reporting_field_set(code, field_num, val)

    elif req.field == "reporting_delete":
        rk = req.reporting_key
        if not rk or rk not in REPORTING_BY_KEY:
            raise HTTPException(status_code=400, detail=f"Unknown reporting_key: {rk!r}")
        meta = REPORTING_BY_KEY[rk]
        field_num = meta[0]
        if field_num not in REPORTING_DELETABLE_FIELDS:
            raise HTTPException(status_code=400, detail=f"Field *{field_num} cannot be deleted")
        cmd = build_reporting_field_delete(code, field_num)

    elif req.field == "keypad":
        kn = req.keypad_num
        if kn is None or not 2 <= kn <= 8:
            raise HTTPException(status_code=400, detail="keypad_num required (2-8)")
        pe = req.partition_enable if req.partition_enable is not None else 0
        snd = req.sound if req.sound is not None else 0
        if not 0 <= pe <= 3:
            raise HTTPException(status_code=400, detail="partition_enable must be 0-3")
        if not 0 <= snd <= 3:
            raise HTTPException(status_code=400, detail="sound must be 0-3")
        field_num = 188 + kn  # keypad 2 → *190, keypad 8 → *196
        # Keypad fields have TWO separate sub-fields (partition/enable
        # and sound), each accepting one digit.  The panel needs time to
        # accept the first digit before receiving the second — sending
        # both digits quickly causes them to be misinterpreted.
        # Phase 1: enter prog mode, navigate to field, send first digit
        cmd_phase1 = f"*99{code}800*{field_num}{pe}"
        ok1 = await ha_client.send_keypress(cmd_phase1)
        if not ok1:
            return JSONResponse({"ok": False, "error": "Failed to send keypad phase 1"})
        await _broadcast_keypress(cmd_phase1, f"configure:{req.field}:phase1")
        # Wait for panel to accept the first sub-field
        await asyncio.sleep(2.0)
        # Phase 2: send second digit + exit
        cmd_phase2 = f"{snd}*99"
        ok2 = await ha_client.send_keypress(cmd_phase2)
        if not ok2:
            return JSONResponse({"ok": False, "error": "Failed to send keypad phase 2"})
        await _broadcast_keypress(cmd_phase2, f"configure:{req.field}:phase2")
        cmd = cmd_phase1 + " (pause) " + cmd_phase2
        already_sent = True

    else:
        raise HTTPException(status_code=400, detail=f"Unknown field: {req.field!r}")

    if not already_sent:
        ok = await ha_client.send_keypress(cmd)
        if ok:
            await _broadcast_keypress(cmd, f"configure:{req.field}")
    else:
        ok = True
    if ok:
        # Update config cache so saved values persist across page refreshes
        if scanner:
            cache = await scanner.load_cache()
            if cache:
                if req.field == "zone" and req.zone_num is not None:
                    from .scanner import HARDWIRED_ZONES
                    zones = cache.setdefault("zones", {})
                    zkey = str(req.zone_num)
                    zd = zones.setdefault(zkey, {"zone": req.zone_num})
                    zd["zone_type"] = req.zone_type or 0
                    zd["partition"] = req.partition if req.partition is not None else 1
                    zd["report_code"] = req.report_code if req.report_code is not None else 1
                    if req.zone_num in HARDWIRED_ZONES:
                        _it = req.input_type if req.input_type is not None else 1
                        zd["input_type"] = _it
                        zd["hw_type"] = _it // 10
                        zd["response_time"] = _it % 10
                    else:
                        # Expansion / wireless zones have no input_type
                        zd.pop("input_type", None)
                        zd.pop("hw_type", None)
                        zd.pop("response_time", None)
                    await scanner._save_cache(cache)
                elif req.field == "zone_name" and req.zone_num is not None:
                    zones = cache.setdefault("zones", {})
                    zkey = str(req.zone_num)
                    zd = zones.setdefault(zkey, {"zone": req.zone_num})
                    # Resolve word IDs to text for the cached name
                    wids = req.word_ids or []
                    # Build a merged vocab including custom words from cache
                    merged_vocab = dict(VOCAB)
                    for _k, _e in (cache.get("custom_words") or {}).items():
                        _wn = _e.get("word_num")
                        _ct = (_e.get("content") or "").strip()
                        if _wn and 1 <= _wn <= 10 and _ct:
                            merged_vocab[244 + _wn] = _ct
                    words = [merged_vocab.get(wid, "") for wid in wids if wid]
                    zd["name"] = " ".join(w for w in words if w)
                    await scanner._save_cache(cache)
                elif req.field == "custom_word":
                    cw = cache.setdefault("custom_words", {})
                    key = f"{req.word_num:02d}"
                    cw[key] = {
                        "word_num": req.word_num,
                        "content": text.upper(),
                        "raw_display": f"CUSTOM? {key} {text.upper()}",
                    }
                    await scanner._save_cache(cache)
                elif req.field in ("fire_timeout", "bell_timeout"):
                    delays = cache.setdefault("delays", {})
                    delays[req.field] = int(req.value)
                    await scanner._save_cache(cache)
                elif req.field in ("exit_delay", "entry_delay_1", "entry_delay_2"):
                    delays = cache.setdefault("delays", {})
                    raw_str = str(req.value).strip()
                    # Store exactly what was sent (4-digit raw or legacy int)
                    delays[req.field] = raw_str if len(raw_str) == 4 and raw_str.isdigit() else int(raw_str)
                    await scanner._save_cache(cache)
                elif req.field in ("reporting", "reporting_delete") and req.reporting_key:
                    rpt = cache.setdefault("reporting", {})
                    rk = req.reporting_key
                    meta = REPORTING_BY_KEY.get(rk)
                    if meta:
                        if req.field == "reporting_delete":
                            if rk in rpt:
                                rpt[rk]["value"] = ""
                                rpt[rk]["raw_display"] = ""
                        else:
                            rpt.setdefault(rk, {"field": meta[0], "label": meta[2]})
                            rpt[rk]["value"] = str(req.value or "")
                            rpt[rk]["raw_display"] = ""
                        await scanner._save_cache(cache)
                elif req.field == "keypad" and req.keypad_num is not None:
                    kpads = cache.setdefault("keypads", {})
                    key = str(req.keypad_num)
                    kpads[key] = {
                        "keypad": req.keypad_num,
                        "address": 15 + req.keypad_num,
                        "field": 188 + req.keypad_num,
                        "partition_enable": f"{req.partition_enable or 0:02d}",
                        "sound": f"{req.sound or 0:02d}",
                        "raw_digits": f"{req.partition_enable or 0:02d}{req.sound or 0:02d}",
                        "raw_display": "",
                    }
                    await scanner._save_cache(cache)
    return {"ok": ok}


# ---------------------------------------------------------------------------
# Routes — user account management
# ---------------------------------------------------------------------------

class UserConfigureRequest(BaseModel):
    action: str              # "change_installer", "change_master", "set_code", "delete", "authority", "partition"
    user_num: int | None = None       # 03-49 for regular users
    new_code: str | None = None       # 4-digit code
    authority_level: int | None = None  # 0-4
    partitions: list[int] | None = None  # [1], [2], [3], [1,2], etc.


@app.post("/api/user_configure")
async def user_configure(req: UserConfigureRequest):
    if not ha_client:
        raise HTTPException(status_code=503, detail="Client not ready")

    cmd: str | None = None

    if req.action == "change_installer":
        code = _require_code()
        if not req.new_code or not req.new_code.isdigit() or len(req.new_code) != 4:
            raise HTTPException(status_code=400, detail="new_code must be 4 digits")
        cmd = build_installer_code_change(code, req.new_code)

    elif req.action == "change_master":
        user_code = getattr(app.state, "session_user_code", None) or _load_user_code()
        if not user_code:
            raise HTTPException(status_code=400, detail="Master/user code not set")
        if not req.new_code or not req.new_code.isdigit() or len(req.new_code) != 4:
            raise HTTPException(status_code=400, detail="new_code must be 4 digits")
        cmd = build_master_code_change(user_code, req.new_code)

    elif req.action == "set_code":
        user_code = getattr(app.state, "session_user_code", None) or _load_user_code()
        if not user_code:
            raise HTTPException(status_code=400, detail="Master/user code not set")
        if req.user_num is None or not 3 <= req.user_num <= 49:
            raise HTTPException(status_code=400, detail="user_num must be 3-49")
        if not req.new_code or not req.new_code.isdigit() or len(req.new_code) != 4:
            raise HTTPException(status_code=400, detail="new_code must be 4 digits")
        cmd = build_user_code_set(user_code, req.user_num, req.new_code)

    elif req.action == "delete":
        user_code = getattr(app.state, "session_user_code", None) or _load_user_code()
        if not user_code:
            raise HTTPException(status_code=400, detail="Master/user code not set")
        if req.user_num is None or not 3 <= req.user_num <= 49:
            raise HTTPException(status_code=400, detail="user_num must be 3-49")
        cmd = build_user_delete(user_code, req.user_num)

    elif req.action == "authority":
        user_code = getattr(app.state, "session_user_code", None) or _load_user_code()
        if not user_code:
            raise HTTPException(status_code=400, detail="Master/user code not set")
        if req.user_num is None or not 3 <= req.user_num <= 49:
            raise HTTPException(status_code=400, detail="user_num must be 3-49")
        if req.authority_level is None or not 0 <= req.authority_level <= 4:
            raise HTTPException(status_code=400, detail="authority_level must be 0-4")
        cmd = build_user_authority(user_code, req.user_num, req.authority_level)

    elif req.action == "partition":
        user_code = getattr(app.state, "session_user_code", None) or _load_user_code()
        if not user_code:
            raise HTTPException(status_code=400, detail="Master/user code not set")
        if req.user_num is None or not 3 <= req.user_num <= 49:
            raise HTTPException(status_code=400, detail="user_num must be 3-49")
        if not req.partitions or not all(1 <= p <= 3 for p in req.partitions):
            raise HTTPException(status_code=400, detail="partitions must be list of 1-3")
        cmd = build_user_partition(user_code, req.user_num, req.partitions)

    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {req.action!r}")

    ok = await ha_client.send_keypress(cmd)
    if ok:
        await _broadcast_keypress(cmd, f"user:{req.action}")
        # Update stored codes if installer or master code was changed
        if req.action == "change_installer" and req.new_code:
            old_user_code = _load_user_code() or ""
            _save_code(req.new_code, old_user_code)
        elif req.action == "change_master" and req.new_code:
            old_code = _load_code() or ""
            _save_code(old_code, req.new_code)
            app.state.session_user_code = req.new_code
    return {"ok": ok}


# ---------------------------------------------------------------------------
# Routes — event log
# ---------------------------------------------------------------------------

class EventLogRequest(BaseModel):
    entries: int = 5  # 1–100


@app.post("/api/eventlog")
async def fetch_eventlog(req: EventLogRequest):
    global _log_capture_queue, _log_capture_count
    if not ha_client:
        raise HTTPException(status_code=503, detail="Client not ready")
    # Event buffer requires the master/user code, NOT the installer code.
    user_code = getattr(app.state, "session_user_code", None) or _load_user_code()
    if not user_code:
        raise HTTPException(
            status_code=400,
            detail="User/master code not set. Enter it in the login modal.",
        )
    count = max(1, min(req.entries, 100))

    # Set up capture queue.  Use count + 5 for both the queue size and the
    # capture limit so that leading/trailing non-log display updates (e.g.
    # the normal keypad state shown just after sending the command, or the
    # exit notification) do not consume slots intended for real log entries.
    _log_capture_queue = asyncio.Queue(maxsize=count + 5)
    _log_capture_count = count + 5

    # Entry: {user_code}#60 shows event 001; each * advances to next entry.
    # Send the initial command first, then each * individually with a delay
    # so the panel has time to render each entry before the next keypress.
    cmd = f"{user_code}#60"
    ok = await ha_client.send_keypress(cmd)
    if not ok:
        _log_capture_queue = None
        _log_capture_count = 0
        raise HTTPException(status_code=500, detail="Failed to send command")
    await _broadcast_keypress(cmd, "eventlog")

    # Send each subsequent * with STEP_DELAY so the panel can render each entry
    _STAR_DELAY = 0.50
    for _ in range(count - 1):
        await asyncio.sleep(_STAR_DELAY)
        await ha_client.send_keypress("*")
    await _broadcast_keypress("*" * (count - 1), "eventlog")

    # Collect display updates.  Stop early if the panel auto-exits the event
    # buffer (e.g. after entry 99) — detected by an entry that does NOT match
    # the event-log display format "NNN [E|R]DDD ...".
    _EVENTLOG_RE = re.compile(r"^\d{3}\s+[ER]\d{3}", re.IGNORECASE)
    results: list[str] = []
    capture_timeout = max(30.0, count * 2.0)
    seen_log_entry = False   # True once the first valid log line has been received
    consecutive_non_log = 0  # counts consecutive non-log displays after first real entry
    # Vista panels can interleave trouble displays (e.g. "COMM. FAILURE") between
    # event-log entries.  Only treat the panel as having exited log mode after
    # _MAX_CONSECUTIVE_NON_LOG non-matching displays in a row; a single stray
    # trouble display is tolerated and skipped.
    _MAX_CONSECUTIVE_NON_LOG = 2
    try:
        deadline = asyncio.get_event_loop().time() + capture_timeout
        while len(results) < count:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                entry = await asyncio.wait_for(
                    _log_capture_queue.get(), timeout=min(remaining, 3.0)
                )
                if not _EVENTLOG_RE.search(entry):
                    if not seen_log_entry:
                        # Pre-log display update (e.g. normal keypad state or trouble
                        # display seen just after the command was sent) — skip.
                        _LOGGER.debug("Event log: skipping pre-log display %r", entry)
                        continue
                    # After real entries have arrived, count consecutive non-log lines.
                    # A single "COMM. FAILURE" or similar trouble display can appear
                    # between entries; tolerate it and keep waiting.
                    consecutive_non_log += 1
                    if consecutive_non_log >= _MAX_CONSECUTIVE_NON_LOG:
                        _LOGGER.info(
                            "Event log: panel exited after %d entries (last non-log=%r)",
                            len(results), entry,
                        )
                        break
                    _LOGGER.debug(
                        "Event log: non-log display %r (consecutive=%d) — waiting",
                        entry, consecutive_non_log,
                    )
                    continue
                # Valid log entry — reset the consecutive counter and record it.
                seen_log_entry = True
                consecutive_non_log = 0
                results.append(entry)
            except asyncio.TimeoutError:
                break
    finally:
        _log_capture_queue = None
        _log_capture_count = 0
        # Exit log mode
        await ha_client.send_keypress("0")

    return {"entries": results}


@app.post("/api/recovery")
async def recovery_exit():
    """
    Emergency recovery: send the documented exit sequence for each known
    Vista 20P programming menu state.  Call this if a failed scan left the
    panel stuck in programming mode and normal operation is blocked.

    Sequence (all steps are harmless if the panel is already in a different state):
      *00  — exit *82 zone-browse / navigate to zone 00 → PROGRAM ALPHA? prompt
      0    — exit alpha mode → data-field mode
      00*  — exit *58/*56 zone mode → data-field mode
      *99  — exit programming mode → normal operating mode
    """
    if not ha_client or not scanner:
        raise HTTPException(status_code=503, detail="Client not ready")
    await scanner.force_exit()
    await _broadcast_keypress("*00 / 0 / 00* / *99", "recovery")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Routes — static data for UI dropdowns
# ---------------------------------------------------------------------------

@app.get("/api/vocab")
async def get_vocab():
    # Start with standard vocabulary (IDs 1-244)
    merged = dict(VOCAB)
    # Merge custom words from config cache: custom word N → vocab ID 244+N
    # (custom word 1 = 245, custom word 2 = 246, ..., custom word 10 = 254)
    # Words 11-12 (partition names) are excluded — not used for zone naming.
    if scanner:
        cache = await scanner.load_cache()
        if cache:
            cw = cache.get("custom_words") or {}
            for key, entry in cw.items():
                wn = entry.get("word_num")
                content = (entry.get("content") or "").strip()
                if wn and 1 <= wn <= 10 and content:
                    merged[244 + wn] = content
    return {"vocab": [{"id": k, "word": v} for k, v in sorted(merged.items())]}


@app.get("/api/zone_types")
async def get_zone_types():
    return {
        "zone_types": [
            {"id": k, "name": v, "desc": ZONE_TYPE_DESCS.get(k, "")}
            for k, v in sorted(ZONE_TYPES.items())
        ]
    }


@app.get("/api/zone_states")
async def get_zone_states():
    if not ha_client or not hasattr(ha_client, "get_zone_bypass_states"):
        # Direct mode has no HA entity states to read bypass from
        return {"zones": {}}
    from .scanner import ALL_ZONES
    bypass = await ha_client.get_zone_bypass_states(ALL_ZONES)
    return {"zones": {str(k): v for k, v in bypass.items()}}


@app.post("/api/bypass_scan")
async def scan_bypass_zones():
    """Read currently bypassed zones using the Vista bypass review sequence.

    Sends {UCODE}6* to enter bypass review, then sends '*' repeatedly to
    scroll through additional bypassed zones.  Handles both display spellings:
    'BYPAS' (common) and 'BYPASS'.

    Returns {"zones": {"1": true/false, ...}, "bypassed": [1, 5, ...]}.
    """
    if not ha_client:
        raise HTTPException(status_code=503, detail="Client not ready")

    user_code = getattr(app.state, "session_user_code", None) or _load_user_code()
    if not user_code or not user_code.isdigit() or len(user_code) < 4:
        raise HTTPException(status_code=400, detail="User code not set")

    bypassed_zones: list[int] = []
    max_scrolls = 50  # safety limit

    def _is_bypass_status_screen(text: str) -> bool:
        normalized = " ".join(text.upper().split())
        return normalized.startswith("DISARMED BYPASS") or normalized.startswith("ARMED BYPASS")

    # Start review with UCODE6* (per Vista keypad sequence)
    first = await ha_client.send_and_capture(f"{user_code}6*", timeout=6.0)
    if first:
        text = first.strip()
        m = re.search(r'(?i)BYPAS{1,2}\s+(\d{1,2})\b', text)
        if m:
            bypassed_zones.append(int(m.group(1)))

    for _ in range(max_scrolls):
        captured = await ha_client.send_and_capture("*", timeout=4.0)
        if not captured:
            break

        # The panel might briefly show bare "BYPAS" before the full
        # "BYPAS XX [name]" display.  If so, wait for the next update.
        text = captured.strip()
        if re.match(r'(?i)^BYPAS\s*$', text):
            captured = await ha_client.wait_for_next_update(timeout=5.0)
            if not captured:
                break
            text = captured.strip()

        if bypassed_zones and _is_bypass_status_screen(text):
            # Returned to the normal status screen with BYPASS lit — done.
            break

        m = re.search(r'(?i)BYPAS{1,2}\s+(\d{1,2})\b', text)
        if m:
            zone_num = int(m.group(1))
            if zone_num in bypassed_zones:
                # Wrapped around — seen all bypassed zones
                break
            bypassed_zones.append(zone_num)
        elif "bypas" not in text.lower():
            # Exited bypass scroll — done
            break

    _LOGGER.info(
        "Bypass scan found %d bypassed zone(s): %s",
        len(bypassed_zones), bypassed_zones,
    )

    # Build per-zone result: True for bypassed, False for all others
    zones_result: dict[str, bool] = {}
    for z in range(1, 49):
        zones_result[str(z)] = z in bypassed_zones

    return {"zones": zones_result, "bypassed": bypassed_zones}
