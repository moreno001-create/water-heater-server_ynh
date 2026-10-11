# Run from the repo root: python tests/test_source.py   (see tests/README.md)
# Where the dump load gets inverter readings: the solar server (eg4-dashboard's /api/live) first, EG4 Monitor when it can't be reached.
import asyncio, sys, tempfile, threading, time
from pathlib import Path
REPO = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, REPO)
from aiohttp import web
import solar as sol

live = dict(t=0, pv=5200, soc=99, charge=200, discharge=0, battery=200, grid=0, inv=500, load=500, v_bat=54.0,
            status="Solar, charging battery", lost=False, source="local", age=2, stale=False, interval=5)
up = dict(server=True)

async def server_live(req):
    if not up["server"]:
        return web.json_response({"detail": "No reading yet"}, status=503)
    return web.json_response(live)

async def eg4(req):
    f = await req.post(); j = lambda **kw: web.json_response(dict(success=True, **kw))
    if req.path.endswith("/login"):
        return j(plants=[{"name": "Home", "inverters": [{"serialNum": "4512345678"}]}])
    return j(ppv=3100, soc=97, pCharge=0, pDisCharge=0, pinv=1200, peps=0, pToUser=0, pToGrid=0, statusText="PV")

def run():
    async def main():
        a = web.Application(); a.router.add_get("/api/live", server_live); r = web.AppRunner(a); await r.setup(); await web.TCPSite(r, "127.0.0.1", 8815).start()
        b = web.Application(); b.router.add_post("/{tail:.*}", eg4); r2 = web.AppRunner(b); await r2.setup(); await web.TCPSite(r2, "127.0.0.1", 8814).start()
        await asyncio.Event().wait()
    asyncio.run(main())
threading.Thread(target=run, daemon=True).start(); time.sleep(1)
sol.EG4Client.__init__.__defaults__ = ("http://127.0.0.1:8814",)

heater = dict(junction_id="hp1", name="Garage HP", supports_hot_water_plus=True,
              supported_modes=[dict(mode=m, supports_hot_water_plus=m in ("hybrid", "heat_pump")) for m in ("hybrid", "heat_pump", "electric", "vacation")],
              status=dict(mode="heat_pump", mode_change_pending=False, is_online=True, hot_water_plus_level=1, setpoint=120, setpoint_max=140, setpoint_pending=False))
calls = []
async def get_heater(jid): return heater
async def set_mode(jid, mode, hwp): calls.append(mode); heater["status"]["mode"] = mode
s = sol.Solar(Path(tempfile.mkdtemp()), lambda: True, get_heater, set_mode, None)

async def main():
    p = s.payload()
    print("nothing set up:", p["configured"], "|", s.why_not(None))
    s.settings.update(solar_url="http://127.0.0.1:8815/", enabled=True)
    await s.tick()
    print("solar server:", s.live["source"], "| soc", s.live["soc"], "| fast", s.fast, "| poll", s.payload()["poll_secs"], "s | configured", s.payload()["configured"])
    for _ in range(2):
        s.last_full = 0; await s.tick()                   # three once-a-minute rounds: dumping starts
    print("dumping:", s.state["dumping"], calls)
    live.update(inv=5500)                                 # a surge, between the once-a-minute rounds
    s.last_full = time.time(); await s.tick()
    print("surge seen by the 5-second check:", not s.state["dumping"], "| enabled", s.settings["enabled"], "|", calls, "|", s.state["events"][-1]["msg"])
    live.update(inv=500); s.set_control(enabled=True)
    up["server"] = False; s.last_full = 0; await s.tick()
    print("server down, no EG4 login:", s.live["source"], "| error:", s.error)
    s.settings.update(eg4_account="a", eg4_password="b"); s.last_full = 0; await s.tick()
    print("server down, EG4 login:", s.live["source"], "| soc", s.live["soc"], "| fast", s.fast, "| note:", s.payload()["server_error"])
    up["server"] = True; live.update(stale=True, age=900); s.last_full = 0; await s.tick()
    print("server reading stale:", s.live["source"], "|", s.server_error)
    live.update(stale=False, age=1, source="cloud"); s.last_full = 0; await s.tick()
    print("server on its own cloud fallback:", s.live["source"], "| fast", s.fast)
    live.update(source="local"); s.last_full = 0; await s.tick()
    print("server back on Wi-Fi:", s.live["source"], "| fast", s.fast, "| note", s.payload()["server_error"])
    rows = s.day()
    print("history rows", len(rows), "| no source/fast fields saved:", all("source" not in r for r in rows))
asyncio.run(main())
