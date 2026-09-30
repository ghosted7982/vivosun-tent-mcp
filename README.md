# vivosun-tent-mcp

MCP connector that lets Claude check your Vivosun grow tent: live temp, RH and VPD, device
online status, every device's settings, 1–7 day climate history, and flags against the bonsai plan's
target config. It can also change a short, fixed list of settings (v2). Runs as one Lambda behind a
function URL in your AWS account.

**Writes are bounded.** `tent_configure` can only set the duct fan mode and auto triggers, the light's
mode (cycle/manual) and cycle level, the humidifier's mode (auto/cycle) and auto target, and the heater's
target temperature (60–72°F), each within a safe range. It can't change any other heater setting or turn
any device on or off. The read tools live in `tent.py`, which `tests/test_offline.py` keeps free of write
code; all writes go through `control.py`.

## How it works
- Your Vivosun login → Vivosun REST API (device list + minute-level climate log)
- Temporary AWS IoT credentials (Cognito, issued by Vivosun) → MQTT over websockets → reads each device's shadow
- The client code in `src/vivosun/` is vendored unmodified from
  [lientry/homeassistant-vivosun-growhub](https://github.com/lientry/homeassistant-vivosun-growhub) (MIT, commit in
  `src/vivosun/__init__.py`); only `const.py` drops its Home Assistant import. When Vivosun changes their app and
  upstream ships a fix, re-copy those files and redeploy.

Unofficial: it uses the same cloud API the phone app uses. It breaks when Vivosun changes auth or
encryption until upstream catches up. Vivosun's in-app alerts stay your backstop.

## Deploy (about 5 minutes)
Needs AWS CLI v2 (logged in to the account/region you want), AWS SAM CLI, python3, openssl.

```bash
# optional: test from your laptop first (read-only)
pip install -r requirements.txt
VIVOSUN_EMAIL=you@example.com VIVOSUN_PASSWORD='…' python3 scripts/smoke.py status

./deploy.sh      # prompts once for your Vivosun login → Secrets Manager; prints the connector URL
```

`deploy.sh` stores `{"email","password","path_token"}` in Secrets Manager secret `vivosun-tent`,
builds a Linux/arm64 bundle (no Docker), deploys stack `vivosun-tent-mcp`, and prints:

```
https://<id>.lambda-url.<region>.on.aws/mcp/<48-hex-token>
```

## Add it to Claude
Settings → Connectors → **Add custom connector** → name it `Vivosun tent` → paste the URL → Add.
Then tell Claude it's connected, and it'll wire `tent_status` into the Sunday list and weekday alerts.

## Security model
- The function URL is public, but anything other than `/mcp/<token>` returns 404. The 192-bit token is the credential,
  so treat the URL like a password. To rotate it: edit `path_token` in the secret, then redeploy or wait for cold starts.
- Since v2 the URL can also change settings (within the limits above), so it matters more that it stays private.
  Kill switch: `WRITES_ENABLED=false ./deploy.sh` makes every write fail before it reaches a device;
  redeploy without it to turn writes back on. Every write and its readback is logged.
- The Vivosun password lives only in Secrets Manager; the Lambda role can read only that secret.
- Reserved concurrency of 2 caps cost and abuse. Logs are kept 14 days and never include credentials.
- Cost: a few invocations a day → effectively $0 Lambda, plus $0.40/mo for the secret.

## Tools
| tool | what |
|---|---|
| `tent_status` | current readings + settings + `flags` vs plan (temp 58–88°F alerts, RH 40–75%, light 50–75%, humidifier 55%, exhaust 82°F/70%, heater 64–68°F, level ≤2, water low, device offline (heater offline is only a note)) |
| `tent_history` | `hours` 1–168: min/max/avg temp/RH/VPD, coverage (gaps = offline), % time outside alert bands |
| `tent_raw_shadow` | unparsed device shadows, for debugging field mappings on your specific hardware |
| `tent_configure` | **writes.** Any of: `duct_fan_mode` (auto/cycle/manual), `duct_fan_temp_max_f` 70–90, `duct_fan_rh_max` 50–85, `light_mode` (cycle/manual), `light_cycle_level_pct` 25–100, `humidifier_mode` (auto/cycle), `humidifier_target_rh` 40–70, `heater_target_f` 60–72. Refuses if the device is offline, then reads the device back and returns before / requested / reported per setting |

Targets live in `TARGETS` at the top of `src/tent.py`.

## Tests
`python3 tests/test_offline.py`: MCP protocol, auth path, parsing/scaling, flags, write allowlist/ranges/payloads,
kill switch. No network needed.

## Remove
`sam delete --stack-name vivosun-tent-mcp && aws secretsmanager delete-secret --secret-id vivosun-tent --force-delete-without-recovery`
