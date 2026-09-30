"""Offline tests: MCP protocol handling + parsing/flags on a synthetic shadow. No network."""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.update(VIVOSUN_EMAIL="x@y.z", VIVOSUN_PASSWORD="pw", MCP_PATH_TOKEN="tok123")
import asyncio
import app, control, tent
from vivosun.shadow import parse_shadow_document

def ev(body, path="/mcp/tok123", method="POST"):
    return {"rawPath": path, "requestContext": {"http": {"method": method}}, "body": json.dumps(body)}

# auth
assert app.handler(ev({}, path="/mcp/wrong"))["statusCode"] == 404
assert app.handler(ev({}, path="/"))["statusCode"] == 404
assert app.handler(ev({}, method="GET"))["statusCode"] == 405
# initialize
r = json.loads(app.handler(ev({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}))["body"])
assert r["result"]["protocolVersion"] == "2025-06-18" and r["result"]["capabilities"]["tools"]
assert app.handler(ev({"jsonrpc":"2.0","method":"notifications/initialized"}))["statusCode"] == 202
r = json.loads(app.handler(ev({"jsonrpc":"2.0","id":2,"method":"tools/list"}))["body"])
names = [t["name"] for t in r["result"]["tools"]]
assert names == ["tent_status","tent_history","tent_raw_shadow","tent_configure"], names
assert [t["name"] for t in r["result"]["tools"] if not t["annotations"]["readOnlyHint"]] == ["tent_configure"]
# tent.py stays read-only: writes live only in control.py
src = open(os.path.join(os.path.dirname(__file__), "..", "src", "tent.py")).read()
assert "publish_shadow_update" not in src and "TOPIC_SHADOW_UPDATE" not in src and "build_" not in src

# tool call with a stubbed fetch
async def fake_status(e, p): return {"flags": ["ok"]}
tent.get_status = fake_status
r = json.loads(app.handler(ev({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"tent_status","arguments":{}}}))["body"])
assert r["result"]["structuredContent"] == {"flags": ["ok"]}
r = json.loads(app.handler(ev({"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"nope"}}))["body"])
assert r["result"]["isError"]

# parsing + flags on a synthetic shadow (values ×100, °C)
doc = {"state": {"reported": {
  "light": {"mode": 1, "lv": 90, "spec": 1},
  "dFan": {"mode": 1, "lv": 30, "auto": {"tMax": 2778, "hMax": 7000, "lvMin": 1, "lvMax": 10}},
  "hmdf": {"on": 1, "mode": 1, "targetHumi": 6000, "waterWarn": 1},
  "heat": {"on": 1, "mode": 1, "lv": 3, "targetTemp": 2400},
}}}
parsed = parse_shadow_document(doc)
settings = tent._summarize_settings(parsed)
sensors = tent._sensor_block({"inTemp": 1300, "inHumi": 3500, "inVpd": 90})
flags = tent._flags(sensors, settings)
print(json.dumps(settings, indent=1)); print(sensors); print("\n".join(flags))
joined = " | ".join(flags)
for needle in ["tent temp 55.4", "RH 35.0", "light at 90%", "humidifier target 60.0", "water low", "heater target 75.2", "heater level 3"]:
    assert needle in joined, needle
assert "duct fan temp trigger" not in joined  # 27.78C = 82.0F matches plan
print("history:", tent._history_stats([{"inTemp": 2200, "inHumi": 5500, "inVpd": 120}, {"inTemp": 1300, "inHumi": 8000, "inVpd": -6666}], 1))

# probe-only setups (no inside sensor) still get temp/RH flags and history
probe_flags = " | ".join(tent._flags(tent._sensor_block({"pTemp": 1300, "pHumi": 3500, "pVpd": 90}), {}))
assert "tent temp 55.4" in probe_flags and "RH 35.0" in probe_flags, probe_flags
h = tent._history_stats([{"pTemp": 2200, "pHumi": 5500, "pVpd": 120}], 1)
assert h["temp_f"]["avg"] == 71.6 and h["rh_pct"]["avg"] == 55.0, h

# --- v2 writes (control.py) ---
p = control.plan_changes({"duct_fan_mode": "auto", "duct_fan_temp_max_f": 82, "duct_fan_rh_max": 70,
                          "humidifier_target_rh": 55})
assert set(p) == {"controller", "humidifier"}
assert control.build_desired(p["controller"]) == {"state": {"desired": {"dFan": {"mode": 1, "auto": {"tMax": 2778, "hMax": 7000}}}}}
assert control.build_desired(p["humidifier"]) == {"state": {"desired": {"hmdf": {"auto": {"tHumi": 5500}}}}}
assert control.build_desired(control.plan_changes({"humidifier_mode": "auto", "humidifier_target_rh": 55})["humidifier"]) == \
    {"state": {"desired": {"hmdf": {"mode": 1, "auto": {"tHumi": 5500}}}}}
assert control.FIELDS["humidifier_mode"].from_raw(2) == "cycle"
assert control.build_desired(control.plan_changes({"light_mode": "cycle", "light_cycle_level_pct": 50})["controller"]) == \
    {"state": {"desired": {"light": {"mode": 1, "cycle": {"lv": 50}}}}}
assert control.FIELDS["light_mode"].from_raw(1) == "cycle" and control.FIELDS["light_mode"].from_raw(0) == "manual"
assert control.build_desired(control.plan_changes({"light_cycle_level_pct": 65})["controller"]) == \
    {"state": {"desired": {"light": {"cycle": {"lv": 65}}}}}
# 82°F round-trips through the raw °C×100 value the device stores
assert control.FIELDS["duct_fan_temp_max_f"].from_raw(2778) == 82.0
# nothing outside the allowlist or its ranges gets through
for bad in [{}, {"heater_target_f": 80}, {"heater_target_f": 55}, {"heater_level": 2}, {"duct_fan_temp_max_f": 95}, {"duct_fan_rh_max": 40},
            {"humidifier_target_rh": 80}, {"light_cycle_level_pct": 10}, {"duct_fan_mode": "off"}, {"humidifier_mode": "manual"}, {"light_mode": "plan"}, {"light_mode": "auto"},
            {"duct_fan_rh_max": True}, {"humidifier_target_rh": "55"}]:
    try:
        control.plan_changes(bad)
        raise AssertionError(f"accepted {bad}")
    except ValueError:
        pass
# the heater's only writable field is its target temperature
assert [(n, f.path) for n, f in control.FIELDS.items() if f.device_type == "heater"] == [("heater_target_f", ("heat", "tTemp"))]
assert control.build_desired(control.plan_changes({"heater_target_f": 66})["heater"]) == \
    {"state": {"desired": {"heat": {"tTemp": 1889}}}}
assert control.FIELDS["heater_target_f"].from_raw(1889) == 66.0
# kill switch refuses before any network call
os.environ["WRITES_ENABLED"] = "false"
try:
    asyncio.run(control.configure("x", "y", {"duct_fan_rh_max": 70}))
    raise AssertionError("write ran with WRITES_ENABLED=false")
except PermissionError:
    pass
del os.environ["WRITES_ENABLED"]
# the MCP schema and the server-side limits agree
schema = next(t for t in app.TOOLS if t["name"] == "tent_configure")["inputSchema"]["properties"]
for name, f in control.FIELDS.items():
    if f.choices is not None:
        assert schema[name]["enum"] == list(f.choices), name
    else:
        assert (schema[name]["minimum"], schema[name]["maximum"]) == (f.lo, f.hi), name
assert set(schema) == set(control.FIELDS)
# settings summary reads the firmware's humidifier target and light cycle level
raw = {"hmdf": {"mode": 2, "auto": {"tHumi": 6000}}, "light": {"mode": 0, "cycle": {"lv": 82}},
       "heat": {"mode": 0, "state": 1, "tTemp": 2500, "lvMax": 100}}
summ = tent._summarize_settings(parse_shadow_document({"state": {"reported": raw}}), raw)
assert summ["humidifier"]["target_rh"] == 60.0 and summ["light"]["cycle_level_pct"] == 82, summ
assert summ["heater"]["target_f"] == 77.0 and summ["heater"]["max_output_pct"] == 100, summ
assert "heater target 77.0°F (plan band 64–68°F)" in tent._flags({}, summ)
print("ALL OFFLINE TESTS PASSED")
