"""Minimal stateless MCP server (Streamable HTTP, JSON responses) on a Lambda function URL.

Read-only: exposes tent status / history / raw shadow. No tool writes to devices.
Auth: the URL path must be /mcp/<path_token>; the token lives in Secrets Manager
next to the Vivosun credentials. Anything else gets a bare 404.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
from typing import Any

import tent

log = logging.getLogger()
log.setLevel(logging.INFO)

SERVER_INFO = {"name": "vivosun-tent", "version": "0.1.0"}
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

TOOLS = [
    {
        "name": "tent_status",
        "title": "Grow tent: current status",
        "description": (
            "Live snapshot of Romas's Vivosun grow tent: temperature (°F), RH, VPD, every device's online state "
            "and settings (light level/spectrum/mode, duct-fan auto thresholds, humidifier target, heater target/level), "
            "plus 'flags' comparing everything to the bonsai plan's target config. Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "tent_history",
        "title": "Grow tent: climate history",
        "description": (
            "Min/max/avg temperature (°F), RH and VPD over the last N hours (1–168, default 24), "
            "sample coverage (gaps = controller offline), and % of time outside alert bands. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"hours": {"type": "integer", "minimum": 1, "maximum": 168, "default": 24}},
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "tent_raw_shadow",
        "title": "Grow tent: raw device shadows (debug)",
        "description": "Unparsed AWS IoT shadow 'reported' state per device. Use only to debug field mappings. Read-only.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
]

_secret_cache: dict[str, str] | None = None


def _secret() -> dict[str, str]:
    global _secret_cache
    if _secret_cache is None:
        if os.environ.get("VIVOSUN_EMAIL"):  # local testing
            _secret_cache = {"email": os.environ["VIVOSUN_EMAIL"], "password": os.environ["VIVOSUN_PASSWORD"],
                             "path_token": os.environ.get("MCP_PATH_TOKEN", "local")}
        else:
            import boto3
            raw = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["SECRET_ID"])["SecretString"]
            _secret_cache = json.loads(raw)
    return _secret_cache


def _http(status: int, body: Any = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    h = {"Content-Type": "application/json", "Cache-Control": "no-store"}
    h.update(headers or {})
    return {"statusCode": status, "headers": h, "body": "" if body is None else json.dumps(body, default=str)}


def _result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    s = _secret()
    try:
        if name == "tent_status":
            data = asyncio.run(tent.get_status(s["email"], s["password"]))
        elif name == "tent_history":
            data = asyncio.run(tent.get_history(s["email"], s["password"], int(args.get("hours", 24))))
        elif name == "tent_raw_shadow":
            data = asyncio.run(tent.get_raw(s["email"], s["password"]))
        else:
            return {"content": [{"type": "text", "text": f"Unknown tool {name}"}], "isError": True}
    except Exception as err:  # surface a clean tool error; never leak credentials
        log.exception("tool %s failed", name)
        return {"content": [{"type": "text", "text": f"{name} failed: {type(err).__name__}: {err}"}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(data, indent=1, default=str)}], "structuredContent": data}


def _handle(msg: dict[str, Any]) -> dict[str, Any] | None:
    method, req_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if req_id is None:  # notification (e.g. notifications/initialized) — no response
        return None
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return _result(req_id, {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                                "serverInfo": SERVER_INFO,
                                "instructions": "Read-only monitor for Romas's bonsai grow tent (Vivosun GrowHub)."})
    if method == "ping":
        return _result(req_id, {})
    if method == "tools/list":
        return _result(req_id, {"tools": TOOLS})
    if method == "tools/call":
        return _result(req_id, _call_tool(params.get("name", ""), params.get("arguments") or {}))
    return _error(req_id, -32601, f"Method not found: {method}")


def handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    path = event.get("rawPath", "")
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    token = _secret().get("path_token", "")
    parts = path.strip("/").split("/")
    if len(parts) != 2 or parts[0] != "mcp" or not token or not hmac.compare_digest(parts[1], token):
        return _http(404, {"error": "not found"})
    if method == "GET":  # no server-initiated stream
        return _http(405, None, {"Allow": "POST, DELETE"})
    if method == "DELETE":
        return _http(204)
    if method != "POST":
        return _http(405, None, {"Allow": "POST"})

    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    try:
        payload = json.loads(body)
    except ValueError:
        return _http(400, _error(None, -32700, "Parse error"))

    if isinstance(payload, list):
        responses = [r for r in (_handle(m) for m in payload if isinstance(m, dict)) if r is not None]
        return _http(200, responses) if responses else _http(202)
    if not isinstance(payload, dict):
        return _http(400, _error(None, -32600, "Invalid request"))
    resp = _handle(payload)
    return _http(202) if resp is None else _http(200, resp)
