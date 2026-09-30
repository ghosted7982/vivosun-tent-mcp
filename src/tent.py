"""Read-only view of Romas's Vivosun grow tent.

Uses the vendored Vivosun client (REST login + device list + point log, then
AWS IoT MQTT over websockets to fetch each device's shadow). Nothing here
publishes to a shadow/update topic: this module cannot change device settings.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict
from typing import Any

import aiohttp

from vivosun.api import VivosunApiClient
from vivosun.aws_auth import AwsAuthClient
from vivosun.const import (
    API_POINT_LOG_PATH,
    TEMP_SCALE_FACTOR,
    TOPIC_SHADOW_GET,
    TOPIC_SHADOW_GET_ACCEPTED,
)
from vivosun.mqtt_client import MQTTClient
from vivosun.shadow import parse_shadow_document

# ---------------------------------------------------------------------------
# Romas's target tent configuration (from the bonsai care plan).
# Used only to flag deviations; never written to the devices.
# ---------------------------------------------------------------------------
TARGETS = {
    "temp_alert_low_f": 58,
    "temp_alert_high_f": 88,
    "temp_comfort_f": (70, 78),
    "rh_alert": (40, 75),
    "rh_comfort": (50, 60),
    "heater_band_f": (64, 68),
    "heater_max_level": 2,
    "humidifier_target_rh": 55,
    "exhaust_temp_f": 82,
    "exhaust_rh": 70,
    "light_level_pct": (50, 75),
}

SHADOW_WAIT_SECONDS = 8.0


def c_to_f(c: float | None) -> float | None:
    return None if c is None else round(c * 9 / 5 + 32, 1)


def scaled(raw: int | None) -> float | None:
    return None if raw is None else raw / TEMP_SCALE_FACTOR


async def _session_bootstrap(session: aiohttp.ClientSession, email: str, password: str):
    api = VivosunApiClient(session)
    tokens = await api.login(email, password)
    devices = await api.get_devices(tokens)
    return api, tokens, devices


async def _fetch_shadows(session, api, tokens, devices) -> dict[str, dict[str, Any]]:
    """Connect to AWS IoT once, request every device's shadow, collect replies."""
    mqtt_devices = [d for d in devices if d.client_id and d.device_type != "camera"]
    if not mqtt_devices:
        return {}
    identity = await api.get_aws_identity(tokens)
    aws = AwsAuthClient(session)
    creds = await aws.get_credentials_for_identity(identity)
    url = aws.sigv4_sign_mqtt_url(endpoint=identity.aws_host, region=identity.aws_region, credentials=creds)

    primary = mqtt_devices[0]
    client = MQTTClient(
        websocket_url=url,
        thing=primary.client_id,
        topic_prefix=primary.topic_prefix,
        client_id=f"romas-mcp-{primary.device_id[:12]}-{uuid.uuid4().hex[:8]}",
        label="mcp",
        keepalive_seconds=30,
    )
    results: dict[str, dict[str, Any]] = {}
    by_topic = {TOPIC_SHADOW_GET_ACCEPTED.format(thing=d.client_id): d for d in mqtt_devices}
    done = asyncio.Event()

    def on_message(topic: str, payload: bytes, qos: int) -> None:
        dev = by_topic.get(topic)
        if dev is None:
            return
        try:
            doc = json.loads(payload)
            results[dev.device_id] = {"parsed": parse_shadow_document(doc), "raw_reported": doc.get("state", {}).get("reported", {})}
        except Exception as err:  # keep going; report per-device error
            results[dev.device_id] = {"error": f"shadow parse failed: {err}"}
        if len(results) >= len(mqtt_devices):
            done.set()

    client.add_message_callback(on_message)
    await client.connect()
    try:
        extra = [(t, 1) for t in by_topic if t not in client.required_topics]
        if extra:
            await client.subscribe(extra)
        for d in mqtt_devices:
            await client.publish(TOPIC_SHADOW_GET.format(thing=d.client_id), b"{}")
        try:
            await asyncio.wait_for(done.wait(), timeout=SHADOW_WAIT_SECONDS)
        except TimeoutError:
            pass
    finally:
        await client.disconnect()
    for d in mqtt_devices:
        results.setdefault(d.device_id, {"error": "no shadow reply (device offline or timeout)"})
    return results


def _sensor_block(snapshot: dict[str, int | None]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label, key in (("inside", "in"), ("outside", "out"), ("probe", "p")):
        t = scaled(snapshot.get(f"{key}Temp"))
        h = scaled(snapshot.get(f"{key}Humi"))
        v = scaled(snapshot.get(f"{key}Vpd"))
        if t is None and h is None and v is None:
            continue
        out[label] = {"temp_f": c_to_f(t), "temp_c": t, "rh_pct": h, "vpd_kpa": v}
    wl = snapshot.get("waterLv")
    if wl is not None:
        out["water_level_raw"] = wl
    return out


def _summarize_settings(parsed: dict[str, Any]) -> dict[str, Any]:
    s: dict[str, Any] = {}
    if "light" in parsed:
        li = parsed["light"]
        s["light"] = {"mode": {0: "manual", 1: "cycle/auto", 2: "plan"}.get(li.get("mode"), li.get("mode")),
                      "level_pct": li.get("level"), "spectrum": li.get("spectrum"), "in_plan": li.get("in_plan")}
    if "dFan" in parsed:
        df = parsed["dFan"]
        a = df.get("auto", {})
        s["duct_fan"] = {"auto": df.get("auto_enabled"), "level": df.get("level"),
                         "auto_thresholds": {
                             "temp_max_f": c_to_f(scaled(a.get("tMax"))), "temp_min_f": c_to_f(scaled(a.get("tMin"))),
                             "rh_max": scaled(a.get("hMax")), "rh_min": scaled(a.get("hMin")),
                             "level_min": a.get("lvMin"), "level_max": a.get("lvMax")}}
    if "cFan" in parsed:
        cf = parsed["cFan"]
        s["circulation_fan"] = {"level": cf.get("level"), "oscillating": cf.get("oscillating"), "night_mode": cf.get("night_mode")}
    if "hmdf" in parsed:
        h = parsed["hmdf"]
        s["humidifier"] = {"on": h.get("on"), "mode": h.get("mode"), "level": h.get("level"),
                           "target_rh": scaled(h.get("target_humidity")), "water_warning": h.get("water_warning")}
    if "heat" in parsed:
        h = parsed["heat"]
        s["heater"] = {"on": h.get("on"), "mode": h.get("mode"), "level": h.get("level"), "state": h.get("state"),
                       "target_f": c_to_f(scaled(h.get("target_temp")))}
    if "plan" in parsed:
        s["growhub_plan_active_stage"] = parsed["plan"].get("active_stage")
    if "connection" in parsed:
        s["connected"] = parsed["connection"].get("connected")
    return s


def _flags(sensors: dict[str, Any], settings: dict[str, Any]) -> list[str]:
    T = TARGETS
    f: list[str] = []
    inside = sensors.get("inside") or sensors.get("probe", {})  # some setups report only the probe
    t, h = inside.get("temp_f"), inside.get("rh_pct")
    if t is not None:
        if t < T["temp_alert_low_f"] or t > T["temp_alert_high_f"]:
            f.append(f"ALERT: tent temp {t}°F outside {T['temp_alert_low_f']}–{T['temp_alert_high_f']}°F")
        elif not (T["temp_comfort_f"][0] - 6 <= t <= T["temp_comfort_f"][1]):
            f.append(f"note: tent temp {t}°F outside comfort band {T['temp_comfort_f']} (night dip to 64°F is fine)")
    if h is not None:
        if h < T["rh_alert"][0] or h > T["rh_alert"][1]:
            f.append(f"ALERT: tent RH {h}% outside {T['rh_alert'][0]}–{T['rh_alert'][1]}%")
    li = settings.get("light")
    if li and li.get("level_pct") is not None and li["level_pct"] > 0:
        lo, hi = T["light_level_pct"]
        if not lo <= li["level_pct"] <= hi:
            f.append(f"light at {li['level_pct']}% (plan: {lo}% week 1, then 65–75%)")
    hm = settings.get("humidifier")
    if hm and hm.get("target_rh") is not None and abs(hm["target_rh"] - T["humidifier_target_rh"]) > 2:
        f.append(f"humidifier target {hm['target_rh']}% (plan: {T['humidifier_target_rh']}%)")
    if hm and hm.get("water_warning"):
        f.append("ALERT: humidifier water low — refill")
    df = settings.get("duct_fan")
    if df:
        if not df.get("auto"):
            f.append("duct fan not in Auto (plan: Advanced Auto)")
        th = df.get("auto_thresholds", {})
        if th.get("temp_max_f") is not None and abs(th["temp_max_f"] - T["exhaust_temp_f"]) > 1.5:
            f.append(f"duct fan temp trigger {th['temp_max_f']}°F (plan: {T['exhaust_temp_f']}°F)")
        if th.get("rh_max") is not None and abs(th["rh_max"] - T["exhaust_rh"]) > 2:
            f.append(f"duct fan RH trigger {th['rh_max']}% (plan: {T['exhaust_rh']}%)")
    ht = settings.get("heater")
    if ht:
        tgt = ht.get("target_f")
        lo, hi = T["heater_band_f"]
        if tgt is not None and not (lo - 1 <= tgt <= hi + 1):
            f.append(f"heater target {tgt}°F (plan band {lo}–{hi}°F)")
        if ht.get("level") is not None and ht["level"] > T["heater_max_level"]:
            f.append(f"heater level {ht['level']} (plan max {T['heater_max_level']})")
    return f


async def get_status(email: str, password: str) -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        api, tokens, devices = await _session_bootstrap(session, email, password)
        now = int(time.time())
        out_devices = []
        shadows = await _fetch_shadows(session, api, tokens, devices)
        all_flags: list[str] = []
        for d in devices:
            sh = shadows.get(d.device_id)
            # REST onlineStatus reads 0 even for live devices; the shadow's MQTT
            # "connected" flag is authoritative when present.
            connected = (sh or {}).get("parsed", {}).get("connection", {}).get("connected")
            online = connected if connected is not None else d.online
            entry: dict[str, Any] = {"name": d.name, "type": d.device_type, "online": online}
            if not online:
                # the heater is switched off outside heating season, so offline is expected
                prefix = "note" if d.device_type == "heater" else "ALERT"
                all_flags.append(f"{prefix}: {d.name} is offline")
            if d.supports_point_log and d.device_type != "camera":
                try:
                    snap = await api.get_point_log(tokens, d, start_time=now - 900, end_time=now)
                    entry["sensors"] = _sensor_block(snap)
                except Exception as err:
                    entry["sensors_error"] = str(err)
            if sh and "parsed" in sh:
                entry["settings"] = _summarize_settings(sh["parsed"])
            elif sh:
                entry["settings_error"] = sh.get("error")
            all_flags += _flags(entry.get("sensors", {}), entry.get("settings", {}))
            out_devices.append(entry)
        return {"as_of_epoch": now, "devices": out_devices, "flags": all_flags or ["all within plan"],
                "targets": TARGETS}


async def get_raw(email: str, password: str) -> dict[str, Any]:
    """Unparsed shadows — for debugging field mappings."""
    async with aiohttp.ClientSession() as session:
        api, tokens, devices = await _session_bootstrap(session, email, password)
        shadows = await _fetch_shadows(session, api, tokens, devices)
        return {"devices": [asdict(d) | {"camera_password": None, "camera_username": None} for d in devices],
                "reported": {k: v.get("raw_reported", v.get("error")) for k, v in shadows.items()}}


async def get_history(email: str, password: str, hours: int = 24) -> dict[str, Any]:
    hours = max(1, min(int(hours), 168))
    async with aiohttp.ClientSession() as session:
        api, tokens, devices = await _session_bootstrap(session, email, password)
        now = int(time.time())
        out = []
        for d in devices:
            if not d.supports_point_log or d.device_type == "camera":
                continue
            rows: list[dict[str, Any]] = []
            start = now - hours * 3600
            while start < now:  # 24 h chunks at 1-minute resolution
                end = min(start + 86400, now)
                data = await api._request_json(  # noqa: SLF001 — reuse the vendored encrypted POST path
                    "POST", API_POINT_LOG_PATH, headers=api._auth_headers(tokens),  # noqa: SLF001
                    json_body={"sceneId": d.scene_id, "deviceId": d.device_id, "startTime": start, "endTime": end,
                               "reportType": 0, "orderBy": "asc", "timeLevel": "ONE_MINUTE"})
                rows += [r for r in data.get("iotDataLogList", []) if isinstance(r, dict)]
                start = end
            out.append({"name": d.name, **_history_stats(rows, hours)})
        return {"hours": hours, "devices": out}


def _history_stats(rows: list[dict[str, Any]], hours: int) -> dict[str, Any]:
    def series(key):
        vals = []
        for r in rows:
            v = r.get(key)
            if isinstance(v, (int, float)) and v != -6666:
                vals.append(v / TEMP_SCALE_FACTOR)
        return vals
    k = "in" if series("inTemp") else "p"  # fall back to the probe when there's no inside sensor
    temps = [c_to_f(v) for v in series(f"{k}Temp")]
    rhs = series(f"{k}Humi")
    vpds = series(f"{k}Vpd")
    T = TARGETS

    def stats(v):
        return None if not v else {"min": round(min(v), 1), "max": round(max(v), 1), "avg": round(sum(v) / len(v), 1)}
    expected = hours * 60
    res = {"samples": len(rows), "coverage_pct": round(100 * len(rows) / expected, 1) if expected else None,
           "temp_f": stats(temps), "rh_pct": stats(rhs), "vpd_kpa": stats(vpds)}
    if temps:
        res["pct_time_temp_below_alert"] = round(100 * sum(t < T["temp_alert_low_f"] for t in temps) / len(temps), 1)
        res["pct_time_temp_above_alert"] = round(100 * sum(t > T["temp_alert_high_f"] for t in temps) / len(temps), 1)
    if rhs:
        res["pct_time_rh_outside_alert"] = round(100 * sum(not (T["rh_alert"][0] <= h <= T["rh_alert"][1]) for h in rhs) / len(rhs), 1)
    return res
