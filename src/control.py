"""Deliberate, bounded writes to Romas's Vivosun grow tent (v2).

Only the settings in FIELDS can change, only within their ranges, and never the heater
or any device's power state. Each call publishes one partial `desired` shadow update per
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


def _f_to_raw_c(f: float) -> int:
    return round((f - 32) * 5 / 9 * TEMP_SCALE_FACTOR)


def _pct_to_raw(pct: float) -> int:  # RH is scaled ×100 like temperatures
    return round(pct * TEMP_SCALE_FACTOR)


class Field(NamedTuple):
    device_type: str
    path: tuple[str, ...]  # location inside the shadow's desired/reported state
    lo: float | None
    hi: float | None
    to_raw: Callable[[Any], int]
    from_raw: Callable[[int], Any]


# Everything tent_configure can touch. No heater entries, no on/off switches.
FIELDS: dict[str, Field] = {
    "duct_fan_mode": Field("controller", ("dFan", "mode"), None, None, DUCT_FAN_MODES.__getitem__,
                           lambda raw: {v: k for k, v in DUCT_FAN_MODES.items()}.get(raw, raw)),
    "duct_fan_temp_max_f": Field("controller", ("dFan", "auto", "tMax"), 70, 90, _f_to_raw_c,
                                 lambda raw: tent.c_to_f(tent.scaled(raw))),
    "duct_fan_rh_max": Field("controller", ("dFan", "auto", "hMax"), 50, 85, _pct_to_raw, tent.scaled),
    "light_cycle_level_pct": Field("controller", ("light", "cycle", "lv"), 25, 100, int, lambda raw: raw),
    "humidifier_target_rh": Field("humidifier", ("hmdf", "auto", "tHumi"), 40, 70, _pct_to_raw, tent.scaled),
}


class Change(NamedTuple):
    setting: str
    field: Field
    value: Any
    raw: int


def plan_changes(args: dict[str, Any]) -> dict[str, list[Change]]:
    """Validate tool arguments into per-device-type changes. Raises ValueError on anything off-list."""
    if not args:
        raise ValueError(f"nothing to change; pass at least one of: {', '.join(FIELDS)}")
    plan: dict[str, list[Change]] = {}
    for name, value in args.items():
        field = FIELDS.get(name)
        if field is None:
            raise ValueError(f"unsupported setting {name!r}; allowed: {', '.join(FIELDS)}")
        if name == "duct_fan_mode":
            if value not in DUCT_FAN_MODES:
                raise ValueError(f"duct_fan_mode must be one of {list(DUCT_FAN_MODES)}")
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not field.lo <= value <= field.hi:
            raise ValueError(f"{name} must be a number from {field.lo} to {field.hi}, got {value!r}")
        plan.setdefault(field.device_type, []).append(Change(name, field, value, field.to_raw(value)))
    return plan


def build_desired(changes: list[Change]) -> dict[str, Any]:
    """Merge one device's changes into a single partial shadow update document."""
    desired: dict[str, Any] = {}
    for c in changes:
        node = desired
        for key in c.field.path[:-1]:
            node = node.setdefault(key, {})
        node[c.field.path[-1]] = c.raw
    return {"state": {"desired": desired}}


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
                if all(_at(after[t], c.field.path) == c.raw for t, cs in plan.items() for c in cs):
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
                            "applied": new == c.raw})
    applied = all(ch["applied"] for ch in changes)
    log.info("tent_configure result applied=%s changes=%s", applied, json.dumps(changes, default=str))
    return {"applied": applied, "changes": changes,
            **({} if applied else {"note": "the cloud accepted the update but the device has not reported the new "
                                           "value; it may still be applying it, or this firmware ignores that field. "
                                           "Check tent_status in a minute."})}
