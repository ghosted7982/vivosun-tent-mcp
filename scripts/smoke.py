"""Live smoke test from your laptop before (or after) deploying.

  VIVOSUN_EMAIL=you@example.com VIVOSUN_PASSWORD='...' python3 scripts/smoke.py [status|history|raw]

Read-only. Prints what the connector would return.
"""
import asyncio, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import tent

cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
e, p = os.environ["VIVOSUN_EMAIL"], os.environ["VIVOSUN_PASSWORD"]
fn = {"status": lambda: tent.get_status(e, p), "history": lambda: tent.get_history(e, p, 24), "raw": lambda: tent.get_raw(e, p)}[cmd]
print(json.dumps(asyncio.run(fn()), indent=1, default=str))
