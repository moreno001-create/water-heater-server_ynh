"""EG4 inverter monitor and solar dump-load controller.

Reads the inverter (solar, battery, inverter output) from the EG4 Monitor cloud - the same data the EG4 app shows,
uploaded by the inverter's Wi-Fi dongle - and, when the batteries are full and solar is plentiful, switches the water
heater to Electric or Hybrid mode so the surplus heats water instead of being curtailed. The heater goes back to its
previous mode when the surplus ends. Optionally the setpoint is raised while dumping (so a
full tank keeps absorbing heat) and put back afterwards. If the inverter output gets near its rating, dumping stops at once and the dump
load is turned off (or paused).

The EG4 calls mirror pylxpweb (https://github.com/joyfulhouse/pylxpweb, MIT), which needs Python 3.12+; YunoHost 12
ships 3.11, so this module talks to the same endpoints directly with aiohttp.
"""
import asyncio
import json
import logging
import math
import random
import time
from datetime import datetime
from pathlib import Path

import aiohttp

EG4_URL = "https://monitor.eg4electronics.com"
POLL_SECS = 60
KEEP_DAYS = 60
START_STREAK = 3       # readings in a row that must allow dumping before it starts
DISCHARGE_STREAK = 2   # readings in a row of battery discharge before it stops
MAX_READ_FAILS = 3     # stop dumping when the inverter can't be read this many times in a row
ELEMENT_W = 4500       # what the heater can add to the inverter's output in Electric mode
log = logging.getLogger("uvicorn.error")

DEFAULTS = dict(eg4_account="", eg4_password="", serial="",
                enabled=False, dump_mode="electric", heater="", boost=False, boost_setpoint=130,
                start_soc=98, stop_soc=90, min_pv=1500, max_discharge=300,
                rated_w=6000, limit_pct=90, overload_action="disable", pause_min=30, min_switch_min=10)
CONTROL_KEYS = ("enabled", "dump_mode", "heater", "boost", "boost_setpoint")


class EG4Error(Exception):
    pass


class EG4AuthError(EG4Error):
    pass


class EG4Client:
    LOGIN = "/WManage/api/login"

    def __init__(self, account, password, base_url=EG4_URL):
        self.account, self.password, self.base = account, password, base_url
        self.http = None
        self.logged_in = False
        self.inverters = []

    async def _post(self, path, data, relogin=True):
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25))
        try:
            async with self.http.post(self.base + path, data=data, headers={"Accept": "application/json"}) as r:
                if r.status == 401:
                    raise aiohttp.ContentTypeError(r.request_info, r.history)
                r.raise_for_status()
                j = await r.json()
        except aiohttp.ContentTypeError:  # session expired: the portal answers with its login page
            if relogin:
                await self.login()
                return await self._post(path, data, relogin=False)
            raise EG4AuthError("EG4 rejected the session")
        if isinstance(j, dict) and j.get("success") is False:
            msg = j.get("msg") or j.get("message") or "request failed"
            raise (EG4AuthError if path == self.LOGIN else EG4Error)(f"EG4: {msg}")
        return j

    async def login(self):
        self.logged_in = False
        j = await self._post(self.LOGIN, dict(account=self.account, password=self.password, language="ENGLISH"), relogin=False)
        self.inverters = [dict(serial=i["serialNum"], plant=p.get("name", ""), rating=i.get("powerRating"))
                          for p in j.get("plants", []) for i in p.get("inverters", []) if i.get("serialNum")]
        self.logged_in = True
        return self.inverters

    async def runtime(self, serial):
        if not self.logged_in:
            await self.login()
        return await self._post("/WManage/api/inverter/getInverterRuntime", {"serialNum": serial})

    async def close(self):
        if self.http and not self.http.closed:
            await self.http.close()


def normalize(rt):
    """EG4 runtime -> the few numbers the dashboard and controller use (watts, %)."""
    g = lambda k: int(float(rt.get(k) or 0))
    pv = g("ppv") or g("ppv1") + g("ppv2") + g("ppv3")
    return dict(t=round(time.time()), pv=pv, soc=rt.get("soc"), charge=g("pCharge"), discharge=g("pDisCharge"),
                inv=max(g("pinv"), g("peps")), grid_in=g("pToUser"), grid_out=g("pToGrid"),
                status=rt.get("statusText") or "", lost=bool(rt.get("lost")))


# --------------------------------------------------------------------------- controller
class Solar:
    """Polls the inverter, logs it, and runs the dump-load rules. Heater access comes from the server:
    heater_ready() -> bool, get_heater(jid) -> device dict (fresh), set_heater_mode(jid, mode, hwp)."""

    def __init__(self, store: Path, heater_ready, get_heater, set_heater_mode, set_heater_setpoint):
        self.store = store
        self.settings_file = store / "solar-settings.json"
        self.state_file = store / "solar-state.json"
        self.log_file = store / "solar.jsonl"
        self.heater_ready, self.get_heater, self.set_heater_mode = heater_ready, get_heater, set_heater_mode
        self.set_heater_setpoint = set_heater_setpoint
        self.settings = dict(DEFAULTS, **self._load(self.settings_file))
        self.state = dict(dict(dumping=False, since=None, restore_mode=None, restore_hwp=None, lockout_until=0,
                               boosted=False, boost_to=None, restore_setpoint=None,
                               last_switch=0, events=[]), **self._load(self.state_file))
        self.client = None
        self.live = None        # last normalized reading
        self.error = None       # last read error
        self.fails = 0
        self.start_streak = self.discharge_streak = 0
        self.wake = asyncio.Event()
        self.task = None

    # ---- persistence
    @staticmethod
    def _load(path):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, path, data, private=False):
        self.store.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        if private:
            tmp.chmod(0o600)  # holds the EG4 password
        tmp.replace(path)

    def save_settings(self, new):
        old = self.settings
        if not new.get("eg4_password"):
            new["eg4_password"] = old["eg4_password"]
        self.settings = dict(DEFAULTS, **{k: new.get(k, old[k]) for k in DEFAULTS})
        if (self.settings["eg4_account"], self.settings["eg4_password"]) != (old["eg4_account"], old["eg4_password"]):
            self._drop_client()
        if self.settings["serial"] != old["serial"]:
            self.live = None
        self._save(self.settings_file, self.settings, private=True)
        self.wake.set()

    def set_control(self, enabled=None, dump_mode=None, heater=None, boost=None, boost_setpoint=None):
        if enabled is not None:
            self.settings["enabled"] = bool(enabled)
            self.event("Dump load turned on" if enabled else "Dump load turned off")
            if enabled:
                self.state["lockout_until"] = 0
        if dump_mode is not None:
            self.settings["dump_mode"] = dump_mode
        if heater is not None:
            self.settings["heater"] = heater
        if boost_setpoint is not None:
            self.settings["boost_setpoint"] = int(boost_setpoint)
        if boost is not None:
            self.settings["boost"] = bool(boost)
            self.event(f"Setpoint boost turned on ({self.settings['boost_setpoint']}°)" if boost else "Setpoint boost turned off")
        self._save(self.settings_file, self.settings, private=True)
        self.wake.set()

    def event(self, msg):
        log.info("Solar dump load: %s", msg)
        self.state["events"] = (self.state["events"] + [dict(t=round(time.time()), msg=msg)])[-40:]
        self._save(self.state_file, self.state)

    def _drop_client(self):
        if self.client:
            asyncio.create_task(self.client.close())
        self.client = None
        self.live = None

    # ---- EG4
    async def connect(self, account, password):
        """Log in with the given (or saved) account and list its inverters, without saving anything."""
        c = EG4Client(account or self.settings["eg4_account"], password or self.settings["eg4_password"])
        try:
            return await c.login()
        finally:
            await c.close()

    async def read(self):
        s = self.settings
        if not (s["eg4_account"] and s["eg4_password"]):
            return None
        if self.client is None:
            self.client = EG4Client(s["eg4_account"], s["eg4_password"])
        if not s["serial"]:
            inv = self.client.inverters or await self.client.login()
            if not inv:
                raise EG4Error("No inverter found on this EG4 account")
            s["serial"] = inv[0]["serial"]
            self._save(self.settings_file, s, private=True)
        return normalize(await self.client.runtime(s["serial"]))

    # ---- loop
    def start(self):
        if not self.task:
            self.task = asyncio.create_task(self._run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            self.task = None
        if self.client:
            await self.client.close()

    async def _run(self):
        last_prune = 0
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Solar tick failed")
            if time.time() - last_prune > 86400:
                last_prune = time.time()
                self._prune()
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), POLL_SECS)
            except asyncio.TimeoutError:
                pass

    async def tick(self):
        reading = None
        try:
            reading = await self.read()
            self.error, self.fails = None, 0
        except EG4AuthError as e:
            self.error, self.fails = f"{e}. Check the EG4 account in Solar settings.", self.fails + 1
            self._drop_client()
        except Exception as e:
            self.error, self.fails = f"Couldn't read the inverter: {e or type(e).__name__}", self.fails + 1
        if reading:
            self.live = reading
            self._append(dict(reading, dump=self.state["dumping"]))
        await self.control(reading)

    async def control(self, r):
        s, st, now = self.settings, self.state, time.time()
        limit = s["rated_w"] * s["limit_pct"] / 100
        if st["dumping"]:
            if not s["enabled"]:
                return await self._stop("dump load turned off")
            if r is None:
                if self.fails >= MAX_READ_FAILS or not (s["eg4_account"] and s["eg4_password"]):
                    await self._stop("the inverter couldn't be read, so stopping to be safe")
                return
            if r["inv"] >= limit:
                await self._stop(f"inverter output {r['inv']:,} W reached {s['limit_pct']}% of {s['rated_w']:,} W")
                self._overload_action()
                return
            if not self.heater_ready():
                return
            d = await self.get_heater(st["heater"])
            if not d["status"]["mode_change_pending"] and d["status"]["mode"] != st["dump_mode"]:
                self.event(f"Heater was switched to {d['status']['mode'].replace('_', ' ')} by hand, so the dump load stepped aside")
                if not await self._unboost(d):
                    return
                st.update(dumping=False, since=None, last_switch=now)
                self._save(self.state_file, st)
                return
            if s["boost"] and not st["boosted"]:
                await self._boost(d)
            elif not s["boost"] and st["boosted"]:
                await self._unboost(d)
            if r["soc"] is not None and r["soc"] < s["stop_soc"]:
                return await self._stop(f"battery fell to {r['soc']}%")
            self.discharge_streak = self.discharge_streak + 1 if r["discharge"] > s["max_discharge"] else 0
            if self.discharge_streak >= DISCHARGE_STREAK:
                return await self._stop(f"battery discharging {r['discharge']:,} W: solar no longer covers the load")
            return
        # not dumping: should it start?
        ok = self.why_not(r) is None
        self.start_streak = self.start_streak + 1 if ok else 0
        if ok and self.start_streak >= START_STREAK:
            await self._start(r)

    def why_not(self, r):
        """Reason dumping can't start now, or None."""
        s, st, now = self.settings, self.state, time.time()
        if not s["enabled"]:
            return "Dump load is off"
        if not (s["eg4_account"] and s["eg4_password"]):
            return "Connect your EG4 account in Solar settings"
        if r is None:
            return self.error or "Waiting for inverter data"
        if not self.heater_ready():
            return "Sign in to iCOMM so the server can switch the water heater"
        if now < st["lockout_until"]:
            return f"Paused until {datetime.fromtimestamp(st['lockout_until']).strftime('%I:%M %p').lstrip('0')} after the inverter neared its limit"
        if now - st["last_switch"] < s["min_switch_min"] * 60:
            return "Waiting a few minutes after the last change"
        if r["soc"] is None or r["soc"] < s["start_soc"]:
            return f"Battery {r['soc']}%, needs {s['start_soc']}%"
        if r["pv"] < s["min_pv"]:
            return f"Solar {r['pv']:,} W, needs {s['min_pv']:,} W"
        if r["discharge"] > s["max_discharge"]:
            return f"Battery is discharging {r['discharge']:,} W"
        add = ELEMENT_W if s["dump_mode"] == "electric" else 0
        if r["inv"] + add >= s["rated_w"] * s["limit_pct"] / 100:
            return f"Not enough inverter headroom: {r['inv']:,} W now + {add:,} W element would pass {s['limit_pct']}% of {s['rated_w']:,} W"
        return None

    async def _start(self, r):
        s, st = self.settings, self.state
        jid = s["heater"]
        d = await self.get_heater(jid)
        cur = d["status"]
        if not cur["is_online"]:
            return
        if cur["mode"] == "vacation":
            return self.event("Heater is in vacation mode, so not dumping") if self.start_streak == START_STREAK else None
        if cur["mode"] == s["dump_mode"]:
            return  # already in the dump mode: nothing to switch
        if not any(m["mode"] == s["dump_mode"] for m in d["supported_modes"]):
            return self.event(f"{d['name']} doesn't support {s['dump_mode']} mode") if self.start_streak == START_STREAK else None
        jid, prev_mode, prev_hwp = d["junction_id"], cur["mode"], cur["hot_water_plus_level"]  # before the switch changes them
        await self.set_heater_mode(jid, s["dump_mode"], prev_hwp)
        st.update(dumping=True, since=round(time.time()), heater=jid, dump_mode=s["dump_mode"], restore_mode=prev_mode,
                  restore_hwp=prev_hwp, last_switch=round(time.time()))
        self._save(self.state_file, st)
        self.discharge_streak = 0
        self.event(f"Dumping solar: {d['name']} switched from {prev_mode.replace('_', ' ')} to {s['dump_mode']} "
                   f"(solar {r['pv']:,} W, battery {r['soc']}%)")
        if s["boost"]:
            await self._boost(d)

    async def _boost(self, d):
        """Raise the setpoint for the rest of the dump; remembers the old one."""
        st, cur = self.state, d["status"]
        target = min(self.settings["boost_setpoint"], cur["setpoint_max"])
        before = cur["setpoint"]  # read before the change can update it
        if target <= before:
            return  # already at or above the boost level: leave it
        try:
            await self.set_heater_setpoint(d["junction_id"], target)
        except Exception as e:
            return self.event(f"Couldn't raise the setpoint ({e})")
        st.update(boosted=True, boost_to=target, restore_setpoint=before)
        self._save(self.state_file, st)
        self.event(f"Setpoint raised from {before}° to {target}° while dumping")

    async def _unboost(self, d=None):
        """Put the setpoint back, unless it was changed by hand during the dump. False if it should be retried."""
        st = self.state
        if not st["boosted"]:
            return True
        try:
            d = d or await self.get_heater(st["heater"])
            sp = d["status"]["setpoint"]
            if sp != st["boost_to"] and not d["status"]["setpoint_pending"]:
                self.event(f"Setpoint was changed by hand to {sp}°, so it was left there")
            else:
                await self.set_heater_setpoint(st["heater"], st["restore_setpoint"])
                self.event(f"Setpoint back to {st['restore_setpoint']}°")
        except Exception as e:
            self.event(f"Couldn't put the setpoint back ({e}); will retry")
            return False
        st.update(boosted=False, boost_to=None, restore_setpoint=None)
        self._save(self.state_file, st)
        return True

    async def _stop(self, why):
        st = self.state
        if self.heater_ready():
            if not await self._unboost():
                return
            try:
                await self.set_heater_mode(st["heater"], st["restore_mode"], st["restore_hwp"])
            except Exception as e:
                self.event(f"Couldn't switch the heater back ({e}); will retry")
                return
            self.event(f"Stopped: {why}. Heater back to {st['restore_mode'].replace('_', ' ')}")
        else:
            self.event(f"Stopped: {why}, but iCOMM is signed out, so the heater is still in {st['dump_mode']} mode"
                       + (f" at {st['boost_to']}°" if st["boosted"] else ""))
        st.update(dumping=False, since=None, last_switch=round(time.time()))
        self.start_streak = self.discharge_streak = 0
        self._save(self.state_file, st)

    def _overload_action(self):
        s = self.settings
        if s["overload_action"] == "pause":
            self.state["lockout_until"] = round(time.time() + s["pause_min"] * 60)
            self.event(f"Dump load paused for {s['pause_min']} minutes")
        else:
            s["enabled"] = False
            self._save(self.settings_file, s, private=True)
            self.event("Dump load turned off automatically. Turn it back on when you're ready")

    # ---- history
    def _append(self, row):
        self.store.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def _prune(self):
        cut = time.time() - KEEP_DAYS * 86400
        try:
            lines = self.log_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        keep = []
        for l in lines:
            try:
                if json.loads(l)["t"] >= cut:
                    keep.append(l)
            except (ValueError, KeyError):
                pass
        if len(keep) < len(lines):
            self.log_file.write_text("".join(l + "\n" for l in keep), encoding="utf-8")

    def day(self):
        cut, out = time.time() - 86400, []
        try:
            with self.log_file.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("t", 0) >= cut:
                        out.append({k: r.get(k) for k in ("t", "pv", "soc", "inv", "charge", "discharge", "dump")})
        except OSError:
            pass
        return out

    # ---- API
    def public_settings(self):
        s = dict(self.settings)
        s["has_password"] = bool(s.pop("eg4_password"))
        return s

    def payload(self):
        s, st = self.settings, self.state
        if st["dumping"]:
            status = dict(state="dumping", text=f"Dumping solar into the water heater ({st['dump_mode']} mode) since "
                                                f"{datetime.fromtimestamp(st['since']).strftime('%I:%M %p').lstrip('0')}")
            if st["boosted"]:
                status["text"] += f", setpoint raised to {st['boost_to']}° (back to {st['restore_setpoint']}° after)"
        else:
            why = self.why_not(self.live)
            state = "off" if not s["enabled"] else "paused" if time.time() < st["lockout_until"] else "ready" if why is None else "waiting"
            status = dict(state=state, text=why or "Conditions met; starting shortly")
        return dict(configured=bool(s["eg4_account"] and s["eg4_password"]), live=self.live, error=self.error,
                    control=dict({k: s[k] for k in CONTROL_KEYS}, boosted=st["boosted"], boost_to=st["boost_to"], **status),
                    limit_w=round(s["rated_w"] * s["limit_pct"] / 100), events=st["events"][-8:][::-1],
                    settings=self.public_settings(), day=self.day(), poll_secs=POLL_SECS)


# --------------------------------------------------------------------------- demo
def demo_payload(control):
    """A simulated sunny day so the Solar tab can be tried in demo mode."""
    now = time.time()
    rnd = random.Random(int(now // 86400))
    start = int(now // 300 * 300) - 86400
    rows, soc, dump = [], 45.0, False
    for t in range(start, int(now) + 1, 300):
        h = datetime.fromtimestamp(t).hour + datetime.fromtimestamp(t).minute / 60
        sun = max(0.0, math.sin(math.pi * (h - 6.5) / 13)) if 6.5 < h < 19.5 else 0.0
        load = 450 + 700 * (h > 17) + rnd.uniform(-100, 300)
        avail = 7600 * sun ** 1.4 * rnd.uniform(.8, 1)
        if soc >= 98 and avail > load + 2500 and load + ELEMENT_W < 5400 and control["enabled"]:
            dump = True
        elif dump and (soc < 90 or avail < load + ELEMENT_W - 1500 or load + ELEMENT_W >= 5400):
            dump = False
        use = load + (ELEMENT_W if dump else 0)
        surplus = avail - use
        room = (100 - soc) / 100 * 14300 * 12  # Wh-ish per 5 min scaled for a 14.3 kWh bank
        charge = max(0.0, min(surplus, 5000, room)) if surplus > 0 else 0.0
        discharge = max(0.0, -surplus) if soc > 20 else 0.0
        soc = max(20.0, min(100.0, soc + (charge - discharge) * 5 / 60 / 14300 * 100))
        pv = min(avail, use + charge)
        rows.append(dict(t=t, pv=round(pv), soc=round(soc), inv=round(use), charge=round(charge), discharge=round(discharge), dump=dump))
    last = rows[-1]
    live = dict(last, grid_in=0, grid_out=0, status="Normal (demo)", lost=False)
    state = "dumping" if last["dump"] else "off" if not control["enabled"] else "waiting"
    text = {"dumping": "Dumping solar into the water heater (demo)", "off": "Dump load is off",
            "waiting": f"Battery {last['soc']}%, needs 98%" if last["soc"] < 98 else f"Solar {last['pv']:,} W, waiting for more"}[state]
    boosted = state == "dumping" and control["boost"]
    if boosted:
        text += f", setpoint raised to {control['boost_setpoint']}° (back to 120° after)"
    return dict(configured=True, live=live, error=None, limit_w=5400,
                control=dict(control, state=state, text=text, boosted=boosted, boost_to=control["boost_setpoint"] if boosted else None),
                events=[dict(t=round(now - 3600 * k), msg=m) for k, m in
                        ((2, "Stopped: battery discharging 820 W: solar no longer covers the load. Heater back to hybrid"),
                         (5, "Dumping solar: Garage Heat Pump switched from hybrid to electric (solar 6,240 W, battery 99%)"))],
                settings=dict(DEFAULTS, eg4_account="demo@example.com", serial="DEMO6000XP", has_password=True,
                              **{k: control[k] for k in CONTROL_KEYS}),
                day=rows, poll_secs=POLL_SECS, demo=True)
