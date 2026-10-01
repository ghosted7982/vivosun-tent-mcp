"""Deliberate, bounded writes to Romas's Vivosun grow tent (v2).

Only the settings in FIELDS can change, only within their ranges, and nothing can switch a
device off. The heater exposes only its target temperature. Each call publishes one partial `desired` shadow update per
device, then reads the device's `reported` state back, so a write the firmware ignores
shows up as not applied instead of silently "succeeding".
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Callable, NamedTuple

import aiohttp

import tent
from vivosun.const import (
    CFAN_LEVEL_MAP,
    DFAN_LEVEL_MAP,
    SENSOR_UNAVAILABLE_SENTINEL,
    TEMP_SCALE_FACTOR,
    TOPIC_SHADOW_GET,
    TOPIC_SHADOW_GET_ACCEPTED,
    TOPIC_SHADOW_UPDATE,
    TOPIC_SHADOW_UPDATE_ACCEPTED,
)

log = logging.getLogger()

TOPIC_SHADOW_UPDATE_REJECTED = "$aws/things/{thing}/shadow/update/rejected"
REPLY_WAIT_SECONDS = 8.0
READBACK_ATTEMPTS = 5
READBACK_INTERVAL_SECONDS = 2.0

DUCT_FAN_MODES = {"manual": 0, "auto": 1, "cycle": 2}
# No "manual" for the humidifier: it runs at a fixed output and ignores RH (mold risk).
HUMIDIFIER_MODES = {"auto": 1, "cycle": 2}
# The light and circulation fan number modes differently (1 = cycle, 2 = GrowHub plan). No "plan": none is set up.
LIGHT_MODES = {"manual": 0, "cycle": 1}
CIRC_FAN_MODES = {"manual": 0, "cycle": 1}
MODE_NAMES = {0: "manual", 1: "auto", 2: "cycle"}
LIGHT_MODE_NAMES = {0: "manual", 1: "cycle", 2: "plan"}
ALL_DEVICES = ("controller", "humidifier", "heater")


def _f_to_raw_c(f: float) -> int:
    return round((f - 32) * 5 / 9 * TEMP_SCALE_FACTOR)


def _raw_c_to_f(raw: int) -> float | None:
    return tent.c_to_f(tent.scaled(raw))


def _x100(v: float) -> int:  # RH %, VPD kPa and °C are all stored ×100
    return round(v * TEMP_SCALE_FACTOR)


def _f_delta_to_raw(df: float) -> int:  # a temperature *offset*: no 32° shift
    return round(df * 5 / 9 * TEMP_SCALE_FACTOR)


def _raw_to_f_delta(raw: int) -> float:
    return round(raw * 9 / 5 / TEMP_SCALE_FACTOR, 1)


def _same(raw: Any) -> Any:
    return raw


class Field(NamedTuple):
    device_types: tuple[str, ...]
    path: tuple[str, ...]  # location inside the shadow's desired/reported state
    kind: str  # "number" | "enum" | "bool"
    lo: float | None = None
    hi: float | None = None
    choices: dict[str, int] | None = None
    to_raw: Callable[[Any], int] = int
    from_raw: Callable[[int], Any] = _same
    also: Callable[[int], dict[tuple[str, ...], int]] | None = None  # companion writes derived from the raw value
    note: str = ""
    steps: tuple[int, ...] | None = None  # the only values the firmware accepts; anything else is silently ignored


def _num(dtypes, path, lo, hi, to_raw=int, from_raw=_same, also=None, note="", steps=None) -> Field:
    if steps:
        steps = tuple(v for v in steps if lo <= v <= hi)
        note = f"{note}; one of {list(steps)}".lstrip("; ")
    return Field(dtypes if isinstance(dtypes, tuple) else (dtypes,), path, "number", lo, hi, None,
                 to_raw, from_raw, also, note, steps)


def _mode(dtype: str, key: str, modes: dict[str, int], names: dict[int, str] = MODE_NAMES, note="") -> Field:
    return Field((dtype,), (key, "mode"), "enum", choices=modes, to_raw=modes.__getitem__,
                 from_raw=lambda raw: names.get(raw, raw), note=note)


def _flag(dtypes, path, note="") -> Field:
    return Field(dtypes if isinstance(dtypes, tuple) else (dtypes,), path, "bool", to_raw=int, from_raw=bool, note=note)


def _minutes(dtype, path, lo, hi, note="") -> Field:
    return _num(dtype, path, lo, hi, lambda m: round(m * 60), lambda s: round(s / 60, 2), note=note)


def _temp_f(dtype, path, lo, hi, note="") -> Field:
    return _num(dtype, path, lo, hi, _f_to_raw_c, _raw_c_to_f, note=note)


def _x100_field(dtype, path, lo, hi, note="") -> Field:
    return _num(dtype, path, lo, hi, _x100, tent.scaled, note=note)


# Everything tent_configure can touch, with guardrails: nothing turns a device off (no power switches,
# minimum levels on fans/light/humidifier output), and the heater gets only its target (heat.tTemp,
# confirmed against the app), capped well below anything risky. Hardware wiring (sockets, sensor/probe
# type, time zone, GrowHub plans) is deliberately not here.
FIELDS: dict[str, Field] = {
    # --- duct (exhaust) fan, on the GrowHub ---
    "duct_fan_mode": _mode("controller", "dFan", DUCT_FAN_MODES),
    "duct_fan_temp_max_f": _temp_f("controller", ("dFan", "auto", "tMax"), 70, 90, "auto: speed up above this"),
    "duct_fan_temp_min_f": _temp_f("controller", ("dFan", "auto", "tMin"), 50, 80, "auto: slow down below this"),
    "duct_fan_rh_max": _x100_field("controller", ("dFan", "auto", "hMax"), 50, 85, "auto: speed up above this RH %"),
    "duct_fan_rh_min": _x100_field("controller", ("dFan", "auto", "hMin"), 5, 60, "auto: slow down below this RH %"),
    "duct_fan_vpd_max_kpa": _x100_field("controller", ("dFan", "auto", "vpdMax"), 0.4, 3.0),
    "duct_fan_vpd_min_kpa": _x100_field("controller", ("dFan", "auto", "vpdMin"), 0.1, 2.0),
    "duct_fan_auto_level_min_pct": _num("controller", ("dFan", "auto", "lvMin"), 0, 60, note="auto: idle speed"),
    "duct_fan_auto_level_max_pct": _num("controller", ("dFan", "auto", "lvMax"), 30, 100, note="auto: top speed"),
    "duct_fan_manual_level_pct": _num("controller", ("dFan", "manu", "lv"), 30, 100, steps=DFAN_LEVEL_MAP),
    "duct_fan_cycle_on_level_pct": _num("controller", ("dFan", "cycle", "lvOn"), 30, 100),
    "duct_fan_cycle_off_level_pct": _num("controller", ("dFan", "cycle", "lvOff"), 0, 100),
    "duct_fan_cycle_on_minutes": _minutes("controller", ("dFan", "cycle", "onDur"), 1, 1440),
    "duct_fan_cycle_off_minutes": _minutes("controller", ("dFan", "cycle", "offDur"), 0, 1440),
    # --- circulation (clip) fan, on the GrowHub ---
    "circ_fan_mode": _mode("controller", "cFan", CIRC_FAN_MODES, LIGHT_MODE_NAMES),
    "circ_fan_manual_level_pct": _num("controller", ("cFan", "manu", "lv"), 10, 100, steps=CFAN_LEVEL_MAP),
    "circ_fan_oscillation": _flag("controller", ("cFan", "osc")),
    "circ_fan_night_mode": _flag("controller", ("cFan", "nw"), "quieter while the light is off"),
    "circ_fan_cycle_on_level_pct": _num("controller", ("cFan", "cycle", "lvOn"), 10, 100),
    "circ_fan_cycle_on_minutes": _minutes("controller", ("cFan", "cycle", "onDur"), 1, 1440),
    "circ_fan_cycle_off_minutes": _minutes("controller", ("cFan", "cycle", "offDur"), 0, 1440),
    # --- grow light, on the GrowHub ---
    "light_mode": _mode("controller", "light", LIGHT_MODES, LIGHT_MODE_NAMES,
                        "cycle = on/off schedule at the cycle level; manual = fixed manual level"),
    "light_cycle_level_pct": _num("controller", ("light", "cycle", "lv"), 25, 100),
    "light_cycle_spectrum": _num("controller", ("light", "cycle", "spec"), 0, 100),
    "light_on_time_hour": _num("controller", ("light", "cycle", "tOffset"), 0, 23.75, lambda h: round(h * 3600),
                               lambda s: round(s / 3600, 2), note="schedule start, hours after midnight (6.5 = 6:30)"),
    "light_on_hours": _num("controller", ("light", "cycle", "onDur"), 4, 24, lambda h: round(h * 3600),
                           lambda s: round(s / 3600, 2), also=lambda raw: {("light", "cycle", "offDur"): 86400 - raw},
                           note="hours on per day; off time is set to the rest of the 24 h"),
    "light_ramp_minutes": _minutes("controller", ("light", "cycle", "rate"), 0, 60, "sunrise/sunset fade"),
    "light_manual_level_pct": _num("controller", ("light", "manu", "lv"), 25, 100),
    "light_manual_spectrum": _num("controller", ("light", "manu", "spec"), 0, 100),
    # --- humidifier ---
    "humidifier_mode": _mode("humidifier", "hmdf", HUMIDIFIER_MODES, note="aims for its RH/VPD target only in auto"),
    "humidifier_target_rh": _x100_field("humidifier", ("hmdf", "auto", "tHumi"), 40, 70),
    "humidifier_target_vpd_kpa": _x100_field("humidifier", ("hmdf", "auto", "tVpd"), 0.4, 1.6),
    "humidifier_vpd_switch": _flag("humidifier", ("hmdf", "auto", "vpdSwit"),
                                   "auto mode's VPD switch as the app sets it (meaning unconfirmed)"),
    "humidifier_auto_max_output_pct": _num("humidifier", ("hmdf", "auto", "lvOn"), 10, 100),
    "humidifier_manual_level_pct": _num("humidifier", ("hmdf", "manu", "lv"), 10, 100),
    "humidifier_cycle_on_level_pct": _num("humidifier", ("hmdf", "cycle", "lvOn"), 10, 100),
    "humidifier_cycle_on_minutes": _minutes("humidifier", ("hmdf", "cycle", "onDur"), 1, 1440),
    "humidifier_cycle_off_minutes": _minutes("humidifier", ("hmdf", "cycle", "offDur"), 0, 1440),
    # --- heater: target only ---
    "heater_target_f": _temp_f("heater", ("heat", "tTemp"), 60, 72),
    # --- GrowHub probe alert thresholds (app notifications) ---
    "alert_temp_low_f": _temp_f("controller", ("alert", "pTemp", "low"), 32, 100),
    "alert_temp_high_f": _temp_f("controller", ("alert", "pTemp", "high"), 50, 110),
    "alert_rh_low": _x100_field("controller", ("alert", "pHumi", "low"), 0, 100),
    "alert_rh_high": _x100_field("controller", ("alert", "pHumi", "high"), 0, 100),
    "alert_vpd_low_kpa": _x100_field("controller", ("alert", "pVpd", "low"), 0, 5),
    "alert_vpd_high_kpa": _x100_field("controller", ("alert", "pVpd", "high"), 0, 5),
    # --- GrowHub probe calibration offsets ---
    "probe_temp_offset_f": _num("controller", ("cali", "pTemp"), -10, 10, _f_delta_to_raw, _raw_to_f_delta),
    "probe_rh_offset": _x100_field("controller", ("cali", "pHumi"), -15, 15),
    # --- device preferences (applied to all three devices) ---
    "buzzer": _flag(ALL_DEVICES, ("keyBuz",), "key beeps on every device"),
    "screen_timeout_seconds": _num(ALL_DEVICES, ("blTime",), 10, 600, note="every device's screen"),
}

# (low, high) pairs that must stay ordered when both are set in one call
ORDERED_PAIRS = [
    ("duct_fan_temp_min_f", "duct_fan_temp_max_f"), ("duct_fan_rh_min", "duct_fan_rh_max"),
    ("duct_fan_vpd_min_kpa", "duct_fan_vpd_max_kpa"), ("duct_fan_auto_level_min_pct", "duct_fan_auto_level_max_pct"),
    ("alert_temp_low_f", "alert_temp_high_f"), ("alert_rh_low", "alert_rh_high"),
    ("alert_vpd_low_kpa", "alert_vpd_high_kpa"),
]


def input_schema() -> dict[str, Any]:
    """JSON schema for tent_configure, generated from FIELDS so the two can't drift."""
    props: dict[str, Any] = {}
    for name, f in FIELDS.items():
        if f.kind == "enum":
            prop: dict[str, Any] = {"type": "string", "enum": list(f.choices)}
        elif f.kind == "bool":
            prop = {"type": "boolean"}
        elif f.steps:
            prop = {"type": "integer", "enum": list(f.steps)}
        else:
            prop = {"type": "number", "minimum": f.lo, "maximum": f.hi}
        if f.note:
            prop["description"] = f.note
        props[name] = prop
    return {"type": "object", "properties": props, "minProperties": 1, "additionalProperties": False}


class Change(NamedTuple):
    setting: str
    field: Field
    value: Any
    writes: dict[tuple[str, ...], int]  # every shadow path this change sets, main path first


def plan_changes(args: dict[str, Any]) -> dict[str, list[Change]]:
    """Validate tool arguments into per-device-type changes. Raises ValueError on anything off-list."""
    if not args:
        raise ValueError("nothing to change; pass at least one setting (see tent_settings)")
    plan: dict[str, list[Change]] = {}
    for name, value in args.items():
        field = FIELDS.get(name)
        if field is None:
            raise ValueError(f"unsupported setting {name!r}; see tent_settings for the list")
        if field.kind == "enum":
            if value not in field.choices:
                raise ValueError(f"{name} must be one of {list(field.choices)}, got {value!r}")
        elif field.kind == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false, got {value!r}")
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not field.lo <= value <= field.hi:
            raise ValueError(f"{name} must be a number from {field.lo} to {field.hi}, got {value!r}")
        elif field.steps and value not in field.steps:
            raise ValueError(f"{name} must be one of {list(field.steps)}, got {value!r}")
        raw = field.to_raw(value)
        writes = {field.path: raw, **(field.also(raw) if field.also else {})}
        for dtype in field.device_types:
            plan.setdefault(dtype, []).append(Change(name, field, value, writes))
    for lo, hi in ORDERED_PAIRS:
        if lo in args and hi in args and not args[lo] < args[hi]:
            raise ValueError(f"{lo} ({args[lo]}) must be below {hi} ({args[hi]})")
    return plan


def build_desired(changes: list[Change]) -> dict[str, Any]:
    """Merge one device's changes into a single partial shadow update document."""
    desired: dict[str, Any] = {}
    for c in changes:
        for path, raw in c.writes.items():
            node = desired
            for key in path[:-1]:
                node = node.setdefault(key, {})
            node[path[-1]] = raw
    return {"state": {"desired": desired}}


def describe(reported_by_type: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Every editable setting with its current value and what tent_configure accepts."""
    out: dict[str, Any] = {}
    for name, f in FIELDS.items():
        current = {}
        for dtype in f.device_types:
            raw = _at(reported_by_type.get(dtype, {}), f.path)
            current[dtype] = None if raw is None or raw == SENSOR_UNAVAILABLE_SENTINEL else f.from_raw(raw)
        entry: dict[str, Any] = {"current": current[f.device_types[0]] if len(current) == 1 else current}
        entry["allowed"] = (list(f.choices) if f.kind == "enum" else "true/false" if f.kind == "bool"
                            else list(f.steps) if f.steps else [f.lo, f.hi])
        if f.note:
            entry["note"] = f.note
        out[name] = entry
    return out


def _at(reported: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = reported
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node


class _Shadows:
    """Request/reply helper for shadow get and update on one MQTT connection."""

    def __init__(self, client) -> None:
        self.client = client
        self.queues: dict[str, asyncio.Queue] = {}
        client.add_message_callback(self._on_message)

    def _on_message(self, topic: str, payload: bytes, qos: int) -> None:
        q = self.queues.get(topic)
        if q is not None:
            q.put_nowait((topic, payload))

    async def watch(self, things: list[str]) -> None:
        for thing in things:
            get_q, upd_q = asyncio.Queue(), asyncio.Queue()
            self.queues[TOPIC_SHADOW_GET_ACCEPTED.format(thing=thing)] = get_q
            self.queues[TOPIC_SHADOW_UPDATE_ACCEPTED.format(thing=thing)] = upd_q
            self.queues[TOPIC_SHADOW_UPDATE_REJECTED.format(thing=thing)] = upd_q
        topics = [(t, 1) for t in self.queues if t not in self.client.required_topics]
        if topics:
            await self.client.subscribe(topics)

    async def _request(self, reply_topic: str, publish_topic: str, payload: bytes) -> tuple[str, bytes]:
        q = self.queues[reply_topic]
        while not q.empty():
            q.get_nowait()
        await self.client.publish(publish_topic, payload)
        return await asyncio.wait_for(q.get(), timeout=REPLY_WAIT_SECONDS)

    async def reported(self, thing: str) -> dict[str, Any]:
        _, payload = await self._request(TOPIC_SHADOW_GET_ACCEPTED.format(thing=thing),
                                         TOPIC_SHADOW_GET.format(thing=thing), b"{}")
        return json.loads(payload).get("state", {}).get("reported", {})

    async def update(self, thing: str, document: dict[str, Any]) -> None:
        topic, payload = await self._request(TOPIC_SHADOW_UPDATE_ACCEPTED.format(thing=thing),
                                             TOPIC_SHADOW_UPDATE.format(thing=thing), json.dumps(document).encode())
        if topic.endswith("/rejected"):
            raise RuntimeError(f"shadow update rejected: {payload.decode(errors='replace')[:300]}")


async def settings(email: str, password: str) -> dict[str, Any]:
    """Read-only: current value and allowed range of every setting tent_configure can change."""
    async with aiohttp.ClientSession() as session:
        api, tokens, devices = await tent._session_bootstrap(session, email, password)
        shadows = await tent._fetch_shadows(session, api, tokens, devices)
    by_type = {d.device_type: shadows.get(d.device_id, {}).get("raw_reported", {}) for d in devices}
    return {"devices": {d.device_type: d.name for d in devices}, "settings": describe(by_type)}


async def configure(email: str, password: str, args: dict[str, Any]) -> dict[str, Any]:
    if os.environ.get("WRITES_ENABLED", "true").lower() != "true":
        raise PermissionError("writes are disabled on this deployment (stack parameter WritesEnabled=false)")
    plan = plan_changes(args)

    async with aiohttp.ClientSession() as session:
        api, tokens, devices = await tent._session_bootstrap(session, email, password)
        targets = {}
        for dtype in plan:
            matches = [d for d in devices if d.device_type == dtype and d.client_id]
            if len(matches) != 1:
                raise RuntimeError(f"expected exactly one {dtype} device, found {len(matches)}")
            targets[dtype] = matches[0]

        client = await tent._mqtt_client(session, api, tokens, tent._mqtt_devices(devices))
        shadows = _Shadows(client)
        await client.connect()
        try:
            await shadows.watch([d.client_id for d in targets.values()])
            before = {dtype: await shadows.reported(d.client_id) for dtype, d in targets.items()}
            for dtype, d in targets.items():
                if before[dtype].get("connected") is not True:
                    raise RuntimeError(f"{d.name} is offline; nothing was changed")

            for dtype, d in targets.items():
                document = build_desired(plan[dtype])
                log.info("tent_configure write device=%s desired=%s", d.name, json.dumps(document["state"]["desired"]))
                await shadows.update(d.client_id, document)

            after = dict(before)
            for _ in range(READBACK_ATTEMPTS):
                await asyncio.sleep(READBACK_INTERVAL_SECONDS)
                after = {dtype: await shadows.reported(d.client_id) for dtype, d in targets.items()}
                if all(_at(after[t], path) == raw for t, cs in plan.items() for c in cs for path, raw in c.writes.items()):
                    break
        finally:
            await client.disconnect()

    changes = []
    for dtype, cs in plan.items():
        for c in cs:
            old, new = _at(before[dtype], c.field.path), _at(after[dtype], c.field.path)
            changes.append({"setting": c.setting, "device": targets[dtype].name,
                            "before": None if old is None else c.field.from_raw(old),
                            "requested": c.value,
                            "reported": None if new is None else c.field.from_raw(new),
                            "applied": all(_at(after[dtype], p) == raw for p, raw in c.writes.items())})
    applied = all(ch["applied"] for ch in changes)
    log.info("tent_configure result applied=%s changes=%s", applied, json.dumps(changes, default=str))
    return {"applied": applied, "changes": changes,
            **({} if applied else {"note": "the cloud accepted the update but the device has not reported the new "
                                           "value; it may still be applying it, or this firmware ignores that field. "
                                           "Check tent_status in a minute."})}
