"""Offline tests: MCP protocol handling + parsing/flags on a synthetic shadow. No network."""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.update(VIVOSUN_EMAIL="x@y.z", VIVOSUN_PASSWORD="pw", MCP_PATH_TOKEN="tok123")
import app, tent
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
assert names == ["tent_status","tent_history","tent_raw_shadow"], names
assert all(t["annotations"]["readOnlyHint"] for t in r["result"]["tools"])
# no write tools / no shadow update code paths
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
print("ALL OFFLINE TESTS PASSED")
