"""AO Smith water heater dashboard - web server.

    python server.py            # then open http://127.0.0.1:8765

Run locally, credentials are kept in memory only (never written to disk).

Hosted (e.g. the YunoHost package in this repo), it is configured through environment variables:
    WH_HOST, WH_PORT   address to listen on (default 127.0.0.1:8765)
    WH_DATA_DIR        enables "Stay signed in": the iCOMM login is saved to credentials.json there
                       (mode 600) so the dashboard reconnects after a restart. Also marks the server as
                       running behind a reverse proxy.
"""
import asyncio
import json
import logging
import os
import random
import threading
import webbrowser
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from py_aosmith import (AOSmithAPIClient, AOSmithInvalidCredentialsException,
                        AOSmithInvalidParametersException)
from py_aosmith.models import OperationMode

import monitor as mon
import solar as sol

HOST = os.environ.get("WH_HOST", "127.0.0.1")
PORT = int(os.environ.get("WH_PORT", "8765"))
DATA_DIR = os.environ.get("WH_DATA_DIR")
CRED_FILE = Path(DATA_DIR) / "credentials.json" if DATA_DIR else None
BASE = Path(__file__).parent
STORE = Path(DATA_DIR) if DATA_DIR else BASE  # heating-cycle history and monitor settings
CALL_TIMEOUT = 30  # seconds; the cloud client has no timeout of its own
log = logging.getLogger("uvicorn.error")


# --------------------------------------------------------------------------- backends
def dev_to_dict(d):
    s = d.status
    return dict(
        junction_id=d.junction_id, name=d.name, brand=d.brand, model=d.model, serial=d.serial,
        install_location=d.install_location, device_type=d.device_type.value,
        supports_hot_water_plus=d.supports_hot_water_plus,
        supported_modes=[dict(mode=m.mode.value, has_day_selection=m.has_day_selection,
                              supports_hot_water_plus=m.supports_hot_water_plus) for m in d.supported_modes],
        status=dict(firmware_version=s.firmware_version, is_online=s.is_online, mode=s.current_mode.value,
                    mode_change_pending=s.mode_change_pending, setpoint=s.temperature_setpoint,
                    setpoint_pending=s.temperature_setpoint_pending, setpoint_max=s.temperature_setpoint_maximum,
                    hot_water=s.hot_water_status, hot_water_plus_level=s.hot_water_plus_level))


class LiveBackend:
    kind = "live"

    def __init__(self, email, password):
        self.client = AOSmithAPIClient(email, password)

    async def devices(self):
        return [dev_to_dict(d) for d in await self.client.get_devices()]

    async def energy(self, jid):
        e = await self.client.get_energy_use_data(jid)
        return dict(lifetime_kwh=e.lifetime_kwh, history=[dict(date=h.date, kwh=h.energy_use_kwh) for h in e.history])

    async def set_setpoint(self, jid, value):
        await self.client.update_setpoint(jid, value)

    async def set_mode(self, jid, mode, days, hwp):
        await self.client.update_mode(jid, OperationMode(mode), days, hwp)

    async def close(self):
        await self.client.close()


class DemoBackend:
    """Simulated heaters so the UI can be tried without an account."""
    kind = "demo"

    def __init__(self):
        def modes(*names):
            return [dict(mode=n, has_day_selection=(n == "vacation"), supports_hot_water_plus=(n in ("hybrid", "heat_pump")))
                    for n in names]
        self.devs = {
            "demo-hp": dict(junction_id="demo-hp", name="Garage Heat Pump", brand="A. O. Smith", model="HPTS-50",
                            serial="DEMO-HP-001", install_location="Garage", device_type="heat_pump",
                            supports_hot_water_plus=True, supported_modes=modes("hybrid", "heat_pump", "electric", "vacation"),
                            status=dict(firmware_version="2.1.4", is_online=True, mode="hybrid", mode_change_pending=False,
                                        setpoint=120, setpoint_pending=False, setpoint_max=140, hot_water=None,
                                        hot_water_plus_level=1)),
            "demo-el": dict(junction_id="demo-el", name="Basement Electric", brand="A. O. Smith", model="EE12-50",
                            serial="DEMO-EL-002", install_location="Basement", device_type="re3_connected",
                            supports_hot_water_plus=False, supported_modes=modes("electric", "vacation"),
                            status=dict(firmware_version="1.0.9", is_online=True, mode="electric", mode_change_pending=False,
                                        setpoint=125, setpoint_pending=False, setpoint_max=140, hot_water=None,
                                        hot_water_plus_level=None)),
        }
        self.pending_until = {}

    async def devices(self):
        out = []
        for d in self.devs.values():
            d = json.loads(json.dumps(d))
            d["status"]["hot_water"] = mon.demo_activity(d["junction_id"], d["model"])["demo_hw"]
            if self.pending_until.get(d["junction_id"], 0) > datetime.now().timestamp():
                d["status"]["mode_change_pending"] = d["status"]["setpoint_pending"] = True
            out.append(d)
        return out

    async def energy(self, jid):
        rnd = random.Random(jid)
        base = 4.5 if "hp" in jid else 9.0
        hist = []
        for i in range(60, 0, -1):
            day = date.today() - timedelta(days=i)
            hist.append(dict(date=day.isoformat(), kwh=round(max(0.5, rnd.gauss(base, base * 0.25)), 2)))
        return dict(lifetime_kwh=round(sum(h["kwh"] for h in hist) * 6.1, 1), history=hist)

    async def set_setpoint(self, jid, value):
        d = self.devs[jid]
        if not 95 <= value <= d["status"]["setpoint_max"]:
            raise AOSmithInvalidParametersException("Setpoint out of range")
        d["status"]["setpoint"] = value
        self.pending_until[jid] = datetime.now().timestamp() + 3

    async def set_mode(self, jid, mode, days, hwp):
        d = self.devs[jid]
        info = next((m for m in d["supported_modes"] if m["mode"] == mode), None)
        if not info:
            raise AOSmithInvalidParametersException("Mode not supported by this device")
        if days is not None and not info["has_day_selection"]:
            raise AOSmithInvalidParametersException("Days not supported for this operation mode")
        if days is not None and not 1 <= days <= 100:
            raise AOSmithInvalidParametersException("Invalid days selection")
        d["status"]["mode"] = mode
        if hwp is not None and d["supports_hot_water_plus"]:
            d["status"]["hot_water_plus_level"] = hwp
        self.pending_until[jid] = datetime.now().timestamp() + 3

    async def close(self):
        pass


# --------------------------------------------------------------------------- state
class State:
    backend = None
    email = None
    cache = None
    cache_t = 0.0
    lock = asyncio.Lock()
    login_lock = asyncio.Lock()


S = State()
MON = mon.Monitor(STORE)


def heater_ready():
    return S.backend is not None and S.backend.kind == "live"


async def solar_heater(jid):
    """The water heater the dump load drives: the chosen one, or the first on the account."""
    devs = await devices(fresh=True)
    d = next((d for d in devs if d["junction_id"] == jid), None) if jid else (devs[0] if devs else None)
    if not d:
        raise HTTPException(404, "Unknown water heater")
    return d


async def solar_set_mode(jid, mode, hwp):
    d = await device(jid)
    info = next((m for m in d["supported_modes"] if m["mode"] == mode), None)
    keep_hwp = d["supports_hot_water_plus"] and info and info["supports_hot_water_plus"]
    await call(S.backend.set_mode(jid, mode, None, hwp if keep_hwp else None))
    S.cache = None


async def solar_set_setpoint(jid, value):
    await call(S.backend.set_setpoint(jid, value))
    S.cache = None


SOLAR = sol.Solar(STORE, heater_ready, solar_heater, solar_set_mode, solar_set_setpoint)
DEMO_SOLAR = dict(enabled=True, dump_mode="electric", heater="", boost=False, boost_setpoint=130)


def use_backend(backend, email):
    """Switch the signed-in account; heating cycles are monitored for real accounts only."""
    S.backend, S.email, S.cache = backend, email, None
    MON.stop()
    if backend and backend.kind == "live":
        MON.start(lambda: devices(fresh=True), lambda jid: call(S.backend.energy(jid)))


def load_creds():
    if not CRED_FILE:
        return None
    try:
        c = json.loads(CRED_FILE.read_text())
        return c["email"], c["password"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_creds(email, password):
    CRED_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(CRED_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(dict(email=email, password=password), f)


def clear_creds():
    if CRED_FILE:
        CRED_FILE.unlink(missing_ok=True)


async def try_saved_login():
    """Sign in again with the saved iCOMM login after the server restarts."""
    async with S.login_lock:
        saved = None if S.backend else load_creds()
        if not saved:
            return {}
        extra = dict(saved_email=saved[0])
        backend = LiveBackend(*saved)
        try:
            await call(backend.devices())
        except HTTPException as e:
            await backend.close()
            if e.status_code == 401:
                clear_creds()  # password changed
            else:
                extra["error"] = f"Couldn't reach A. O. Smith: {e.detail}"
            return extra
        use_backend(backend, saved[0])
        return extra


async def devices(fresh=False):
    """Device list with a short cache so the UI polling doesn't hammer the cloud API."""
    if S.backend is None:
        raise HTTPException(401, "Not signed in")
    now = datetime.now().timestamp()
    if fresh or S.cache is None or now - S.cache_t > 10:
        async with S.lock:
            S.cache = await call(S.backend.devices())
            S.cache_t = datetime.now().timestamp()
    return S.cache


async def device(jid, fresh=False):
    d = next((d for d in await devices(fresh) if d["junction_id"] == jid), None)
    if not d:
        raise HTTPException(404, "Unknown water heater")
    return d


async def call(coro):
    try:
        return await asyncio.wait_for(coro, CALL_TIMEOUT)
    except HTTPException:
        raise
    except asyncio.TimeoutError:
        log.error("A. O. Smith request timed out after %ss", CALL_TIMEOUT)
        raise HTTPException(504, "The A. O. Smith service did not respond in time. Try again in a moment.")
    except AOSmithInvalidCredentialsException:
        raise HTTPException(401, "Invalid iCOMM email or password")
    except AOSmithInvalidParametersException as e:
        raise HTTPException(400, str(e) or "Invalid parameters")
    except Exception as e:
        log.exception("A. O. Smith request failed")
        raise HTTPException(502, f"A. O. Smith service error: {e!r}")


# --------------------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(_app):
    if CRED_FILE:  # hosted: reconnect and resume monitoring without waiting for someone to open the page
        extra = await try_saved_login()
        if extra.get("error"):
            log.warning(extra["error"])
    SOLAR.start()
    yield
    MON.stop()
    await SOLAR.stop()


app = FastAPI(title="AO Smith dashboard", lifespan=lifespan)


@app.middleware("http")
async def same_origin_only(request: Request, call_next):
    """Block other websites from driving this API."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        allowed = {f"{HOST}:{PORT}", f"localhost:{PORT}"}
        if DATA_DIR:  # behind a reverse proxy, which only forwards its own domains
            allowed.add(request.headers.get("host"))
        if origin and urlparse(origin).netloc not in allowed:
            return JSONResponse({"detail": "Cross-origin request blocked"}, status_code=403)
    return await call_next(request)


class Login(BaseModel):
    email: str = ""
    password: str = ""
    demo: bool = False
    remember: bool = False


class SetpointIn(BaseModel):
    setpoint: int


class MonitorIn(BaseModel):
    power_url: str = ""
    power_path: str = ""
    power_unit: str = "W"
    power_token: str | None = None  # None keeps the saved token
    tank_gal: int | None = None
    inlet_f: float = 55
    cop: float = 3.0
    on_w: float = 100
    element_w: float = 2000

    def checked(self):
        c = self.model_dump()
        c["power_url"], c["power_path"] = c["power_url"].strip(), c["power_path"].strip()
        if c["power_url"] and not c["power_url"].startswith(("http://", "https://")):
            raise HTTPException(400, "Power meter URL must start with http:// or https://")
        if c["power_url"] and not c["power_path"]:
            raise HTTPException(400, "Enter the JSON field that holds the power reading")
        if c["power_unit"] not in ("W", "kW"):
            raise HTTPException(400, "Unit must be W or kW")
        for k, lo, hi in (("tank_gal", 20, 120), ("inlet_f", 32, 90), ("cop", 1, 6), ("on_w", 1, 2000), ("element_w", 500, 10000)):
            if c[k] is not None and not lo <= c[k] <= hi:
                raise HTTPException(400, f"{k} must be between {lo} and {hi}")
        if c["on_w"] >= c["element_w"]:
            raise HTTPException(400, "The heating threshold must be below the element threshold")
        return c


class SolarControlIn(BaseModel):
    enabled: bool | None = None
    dump_mode: str | None = None
    heater: str | None = None
    boost: bool | None = None
    boost_setpoint: int | None = None


class SolarIn(BaseModel):
    eg4_account: str = ""
    eg4_password: str | None = None  # None keeps the saved password
    serial: str = ""
    start_soc: int = 98
    stop_soc: int = 90
    min_pv: int = 1500
    max_discharge: int = 300
    rated_w: int = 6000
    limit_pct: int = 90
    overload_action: str = "disable"
    pause_min: int = 30
    min_switch_min: int = 10

    def checked(self):
        c = self.model_dump()
        c["eg4_account"], c["serial"] = c["eg4_account"].strip(), c["serial"].strip()
        for k, lo, hi in (("start_soc", 50, 100), ("stop_soc", 10, 100), ("min_pv", 0, 30000), ("max_discharge", 0, 10000),
                          ("rated_w", 1000, 30000), ("limit_pct", 50, 100), ("pause_min", 1, 1440), ("min_switch_min", 1, 120)):
            if not lo <= c[k] <= hi:
                raise HTTPException(400, f"{k} must be between {lo} and {hi}")
        if c["stop_soc"] >= c["start_soc"]:
            raise HTTPException(400, "The stop battery level must be below the start level")
        if c["overload_action"] not in ("disable", "pause"):
            raise HTTPException(400, "Unknown overload action")
        return c


class ModeIn(BaseModel):
    mode: str
    days: int | None = None
    hot_water_plus_level: int | None = None


@app.get("/")
async def index():
    return FileResponse(BASE / "index.html")


@app.get("/api/session")
async def session():
    extra = await try_saved_login()
    return dict(extra, connected=S.backend is not None, kind=S.backend.kind if S.backend else None, email=S.email,
                can_remember=CRED_FILE is not None)


@app.post("/api/login")
async def login(body: Login):
    backend = DemoBackend() if body.demo else LiveBackend(body.email.strip(), body.password)
    try:
        await call(backend.devices())  # validates credentials
    except HTTPException:
        await backend.close()
        raise
    if S.backend:
        await S.backend.close()
    use_backend(backend, "demo" if body.demo else body.email.strip())
    if CRED_FILE and not body.demo and body.remember:
        save_creds(S.email, body.password)
    elif not body.demo:
        clear_creds()
    return await session()


@app.post("/api/logout")
async def logout():
    if S.backend:
        await S.backend.close()
    use_backend(None, None)
    clear_creds()
    return dict(connected=False)


@app.get("/api/devices")
async def list_devices():
    return await devices(fresh=True)


@app.get("/api/devices/{jid}")
async def get_device(jid: str):
    return await device(jid)


@app.get("/api/devices/{jid}/energy")
async def get_energy(jid: str):
    await device(jid)
    return await call(S.backend.energy(jid))


@app.post("/api/devices/{jid}/setpoint")
async def set_setpoint(jid: str, body: SetpointIn):
    d = await device(jid)
    if not 1 <= body.setpoint <= d["status"]["setpoint_max"]:
        raise HTTPException(400, f"Setpoint must be at most {d['status']['setpoint_max']}")
    await call(S.backend.set_setpoint(jid, body.setpoint))
    return await device(jid, fresh=True)


@app.post("/api/devices/{jid}/mode")
async def set_mode(jid: str, body: ModeIn):
    await device(jid)
    await call(S.backend.set_mode(jid, body.mode, body.days, body.hot_water_plus_level))
    return await device(jid, fresh=True)


@app.get("/api/devices/{jid}/activity")
async def get_activity(jid: str):
    d = await device(jid)
    if S.backend.kind == "demo":
        return mon.demo_activity(jid, d["model"])
    MON.devs.setdefault(jid, d)
    return MON.activity(jid)


@app.get("/api/devices/{jid}/monitor")
async def get_monitor(jid: str):
    MON.devs.setdefault(jid, await device(jid))
    return MON.public_config(jid)


@app.post("/api/devices/{jid}/monitor")
async def set_monitor(jid: str, body: MonitorIn):
    MON.devs.setdefault(jid, await device(jid))
    MON.save_config(jid, body.checked())
    return MON.public_config(jid)


@app.post("/api/devices/{jid}/monitor/test")
async def test_monitor(jid: str, body: MonitorIn):
    """Read the power meter once with the settings on screen (before saving them)."""
    await device(jid)
    c = body.checked()
    if not c["power_url"]:
        raise HTTPException(400, "Enter a power meter URL first")
    if c["power_token"] is None:
        c["power_token"] = MON.config(jid)["power_token"]
    try:
        return dict(watts=round(await MON.read_power(c)))
    except Exception as e:
        raise HTTPException(400, f"Couldn't read the power meter: {e or type(e).__name__}")


# --------------------------------------------------------------------------- solar dump load
def demo_session():
    return S.backend is not None and S.backend.kind == "demo"


@app.get("/api/solar")
async def get_solar():
    return sol.demo_payload(DEMO_SOLAR) if demo_session() else SOLAR.payload()


@app.post("/api/solar/control")
async def solar_control(body: SolarControlIn):
    if body.dump_mode is not None and body.dump_mode not in ("electric", "hybrid"):
        raise HTTPException(400, "Dump mode must be electric or hybrid")
    if body.boost_setpoint is not None and not 100 <= body.boost_setpoint <= 140:
        raise HTTPException(400, "Boost setpoint must be between 100 and 140")
    if demo_session():
        DEMO_SOLAR.update({k: v for k, v in body.model_dump().items() if v is not None})
        return sol.demo_payload(DEMO_SOLAR)
    SOLAR.set_control(body.enabled, body.dump_mode, body.heater, body.boost, body.boost_setpoint)
    return SOLAR.payload()


@app.post("/api/solar/settings")
async def solar_settings(body: SolarIn):
    if demo_session():
        raise HTTPException(400, "Solar settings can't be changed in demo mode")
    SOLAR.save_settings(body.checked())
    return SOLAR.payload()


@app.post("/api/solar/connect")
async def solar_connect(body: SolarIn):
    """Log in to EG4 with the account on screen and list its inverters (nothing is saved)."""
    if demo_session():
        return dict(inverters=[dict(serial="DEMO6000XP", plant="Demo", rating=6000)])
    try:
        return dict(inverters=await SOLAR.connect(body.eg4_account.strip(), body.eg4_password))
    except sol.EG4AuthError as e:
        raise HTTPException(400, f"{e}. Check the email and password you use for the EG4 Monitor app.")
    except Exception as e:
        raise HTTPException(502, f"Couldn't reach EG4 Monitor: {e or type(e).__name__}")

if __name__ == "__main__":
    if not DATA_DIR:  # running on the desktop
        threading.Timer(1.2, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
