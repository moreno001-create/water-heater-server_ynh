# Run from the repo root: python tests/test_boost.py   (see tests/README.md)
import sys, tempfile
from pathlib import Path
REPO = str(Path(__file__).resolve().parent.parent)
DATA = tempfile.mkdtemp()
import sys, asyncio
sys.path.insert(0, REPO)
import solar as sol
from pathlib import Path
heater = dict(junction_id="hp1", name="Garage HP", supports_hot_water_plus=True,
              supported_modes=[dict(mode=m, supports_hot_water_plus=m in ("hybrid", "heat_pump")) for m in ("hybrid", "heat_pump", "electric", "vacation")],
              status=dict(mode="heat_pump", mode_change_pending=False, is_online=True, hot_water_plus_level=1, setpoint=120, setpoint_max=140, setpoint_pending=False))
calls = []
async def get_heater(jid): return heater
async def set_mode(jid, mode, hwp): calls.append(("mode", mode)); heater["status"]["mode"] = mode
async def set_sp(jid, v): calls.append(("sp", v)); heater["status"]["setpoint"] = v
s = sol.Solar(Path(DATA), lambda: True, get_heater, set_mode, set_sp)
s.settings.update(eg4_account="a", eg4_password="b", serial="X", enabled=True)
s.set_control(boost=True, boost_setpoint=135)
R = lambda soc, dis=0: dict(t=0, pv=5000, soc=soc, inv=500, charge=0, discharge=dis, grid_in=0, grid_out=0, status="", lost=False)
async def run(r, n=1):
    for _ in range(n):
        s.live = r; await s.control(r)
async def main():
    await run(R(99), 3); print("start:", calls, "|", s.payload()["control"]["text"])
    calls.clear(); await run(R(85)); print("stop:", calls, heater["status"])
    # manual setpoint change during dump is respected
    s.state["last_switch"] = 0; calls.clear()
    await run(R(99), 3); heater["status"]["setpoint"] = 125
    await run(R(85)); print("hand-changed sp:", calls, "| events:", [e["msg"] for e in s.state["events"][-3:]])
    # boost turned off mid-dump restores immediately
    heater["status"].update(setpoint=120, mode="heat_pump"); s.state["last_switch"] = 0; calls.clear()
    await run(R(99), 3); s.set_control(boost=False); await run(R(99)); print("boost off mid-dump:", calls, "dumping", s.state["dumping"])
    # boost capped at device max
    heater["status"].update(setpoint=120, setpoint_max=130, mode="heat_pump"); s.set_control(boost=True, boost_setpoint=140)
    await run(R(85)); s.state["last_switch"] = 0; calls.clear(); await run(R(99), 3); print("capped:", calls)
asyncio.run(main())
