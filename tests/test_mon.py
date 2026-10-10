# Run from the repo root: python tests/test_mon.py   (see tests/README.md)
import sys, tempfile
from pathlib import Path
REPO = str(Path(__file__).resolve().parent.parent)
DATA = tempfile.mkdtemp()
import os, sys, time, json, asyncio
os.environ["WH_DATA_DIR"] = DATA
sys.path.insert(0, REPO)
import server, monitor as m
from fastapi.testclient import TestClient
with TestClient(server.app, base_url="http://wh.example.org") as c:
    c.post("/api/login", json={"demo": True})
    a = c.get("/api/devices/demo-hp/activity").json()
    print("demo est", a["estimate"]["level"], "today[7]", a["today"][7], "typical?", a["typical"] is not None, "daily", a["daily"][-1])
    print("monitor cfg", c.get("/api/devices/demo-hp/monitor").json())
    print("bad cfg", c.post("/api/devices/demo-hp/monitor", json={"power_url": "ftp://x"}).json())
    print("save cfg", c.post("/api/devices/demo-hp/monitor", json={"power_url": "http://127.0.0.1:9/x", "power_path": "a.0.p", "power_token": "tok"}).json())
    print("token stored", json.load(open(os.path.join(DATA, "monitor-settings.json")))["demo-hp"]["power_token"])
    print("keep token", c.post("/api/devices/demo-hp/monitor", json={"power_url": "http://127.0.0.1:9/x", "power_path": "a.0.p"}).json()["has_token"])
    print("test unreachable", c.post("/api/devices/demo-hp/monitor/test", json={"power_url": "http://127.0.0.1:9/x", "power_path": "p"}).json())
print("json_path", m.json_path({"emeters": [{"power": "431.5"}]}, "emeters.0.power"))

# live-mode monitor: feed fake meter readings and cloud samples through Monitor directly
store = m.Path(DATA) / "live"; mon = m.Monitor(store)
mon.devs = {"j1": {"junction_id": "j1", "model": "HPTS-50", "status": {"setpoint": 120, "hot_water": 100}}}
t0 = time.time() - 3 * 3600
tr = mon._tracker("j1", "meter")
for i in range(0, 3 * 3600, 30):
    w = 450 if 1800 <= i < 1800 + 5400 else (4500 if 9000 <= i < 9600 else 3)
    mon._feed("j1", tr, t0 + i, w)
cy = mon.cycles("j1")
print("cycles", [(c["kind"], round((c["e"] - c["s"]) / 60), c["kwh"], c["th"]) for c in cy])
a = mon.activity("j1"); print("live est", a["estimate"]["level"], a["estimate"]["source"], "day cycles", len(a["day"]["cycles"]))

# cloud counter source
async def go():
    t = [0]
    hist = {"v": 10.0}
    async def energy(j): return {"history": [{"date": "2026-10-10", "kwh": hist["v"]}]}
    mon2 = m.Monitor(m.Path(DATA) / "cloud"); mon2.devs = dict(mon.devs)
    now = time.time()
    for k, add in enumerate([0, 0.1, 0.1, 0.0, 0.0, 5.0]):
        hist["v"] += add
        await mon2._counter_tick("j1", energy, now + k * 300)
    tr2 = mon2.trackers["j1"]
    print("cloud on?", tr2.on, "cycles", [(c["kind"], round((c["e"] - c["s"]) / 60)) for c in mon2.cycles("j1")])
asyncio.run(go())
