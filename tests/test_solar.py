# Run from the repo root: python tests/test_solar.py   (see tests/README.md)
import sys, tempfile
from pathlib import Path
REPO = str(Path(__file__).resolve().parent.parent)
DATA = tempfile.mkdtemp()
import sys, asyncio, time
sys.path.insert(0, REPO)
import solar as sol
from pathlib import Path
heater = dict(junction_id="hp1", name="Garage HP", supports_hot_water_plus=True,
              supported_modes=[dict(mode=m, supports_hot_water_plus=m in ("hybrid", "heat_pump")) for m in ("hybrid", "heat_pump", "electric", "vacation")],
              status=dict(mode="heat_pump", mode_change_pending=False, is_online=True, hot_water_plus_level=1))
calls = []
async def get_heater(jid): return heater
async def set_mode(jid, mode, hwp): calls.append((mode, hwp)); heater["status"]["mode"] = mode
s = sol.Solar(Path(DATA), lambda: True, get_heater, set_mode, None)
s.settings.update(eg4_account="a", eg4_password="b", serial="X", enabled=True)
def R(pv, soc, inv, dis=0): return dict(t=0, pv=pv, soc=soc, inv=inv, charge=0, discharge=dis, grid_in=0, grid_out=0, status="", lost=False)
async def step(r, label):
    s.live = r; await s.control(r)
    print(f"{label:28s} dumping={s.state['dumping']!s:5} enabled={s.settings['enabled']!s:5} why={s.why_not(r)!r}")
async def main():
    await step(R(5000, 95, 900), "soc too low")
    for i in range(3): await step(R(5000, 99, 500), f"full+sun #{i+1}")
    print("calls", calls)
    await step(R(5500, 99, 5000), "dumping at 5000 (<5400)")
    await step(R(5500, 99, 5450), "inv over limit 5450")
    print("calls", calls, "events", [e["msg"] for e in s.state["events"]][-2:])
    # re-enable, pause action, discharge stop
    s.set_control(enabled=True); s.state["last_switch"] = 0
    for i in range(3): await step(R(5000, 99, 500), f"again #{i+1}")
    await step(R(3000, 98, 5000, dis=600), "discharging 1")
    await step(R(3000, 97, 5000, dis=700), "discharging 2")
    print("calls", calls)
    # manual override
    s.state["last_switch"] = 0
    for i in range(3): await step(R(5000, 99, 500), f"start #{i+1}")
    heater["status"]["mode"] = "hybrid"
    await step(R(5000, 99, 4800), "user switched to hybrid")
    print("last event:", s.state["events"][-1]["msg"])
    # headroom check: house already at 1500 W -> 1500+4500 >= 5100
    heater["status"]["mode"] = "heat_pump"; s.state["last_switch"] = 0
    await step(R(6000, 100, 1500), "headroom blocks")
    print("payload state", s.payload()["control"]["state"], "| has_password", s.payload()["settings"]["has_password"], "| pw in payload", "b" == s.payload()["settings"].get("eg4_password"))
    d = sol.demo_payload(dict(enabled=True, dump_mode="electric", heater="", boost=False, boost_setpoint=130))
    print("demo rows", len(d["day"]), "dump rows", sum(r["dump"] for r in d["day"]), "max inv", max(r["inv"] for r in d["day"]), d["control"]["state"])
asyncio.run(main())
