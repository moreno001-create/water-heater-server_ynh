"""Heating-cycle monitor and hot-water estimate.

The iCOMM cloud reports a hot-water level but no on/off state, so heating cycles come from one of two sources:
  meter  - a power reading for the heater's circuit (Shelly EM, Home Assistant sensor, Tasmota...), polled every
           POWER_SECS. Accurate on/off times and tells the heat pump (a few hundred W) from the elements (~4.5 kW).
  cloud  - the iCOMM kWh counter for today, polled every SAMPLE_SECS. Coarse: an interval is "on" when the counter rose.

Each source feeds a Tracker, which keeps an energy balance of the tank: heating adds heat (electric power x COP for
the heat pump, x1 for the elements), standby loss removes it, and the heat drawn as hot water is inferred from how
long the heater runs. When a cycle starts, the tank is assumed to be short by a typical cycle's worth of heat
(learned from past cycles); while heating, the shortfall shrinks; when the heater stops, the tank is full.
"""
import asyncio
import json
import logging
import math
import random
import re
import statistics
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import aiohttp

SAMPLE_SECS = 300  # cloud poll; a sample is logged for the charts at this rate
POWER_SECS = 30    # power meter poll
KEEP_DAYS = 60
TREND_DAYS = 30
STANDBY_F_PER_H = 0.4  # typical tank heat loss
MIN_FLOOR = 0.03       # while heating the tank is never shown as completely full
KINDS = ("heat_pump", "element")
log = logging.getLogger("uvicorn.error")

DEFAULTS = dict(power_url="", power_path="", power_unit="W", power_token="",
                tank_gal=None, inlet_f=55, cop=3.0, on_w=100, element_w=2000)


def default_tank_gal(model):
    m = re.search(r"-(\d{2,3})", model or "")
    return min(120, max(20, int(m.group(1)))) if m else 50


def capacity_kwh(gal, setpoint_f, inlet_f):
    """Heat to bring a full tank from inlet to setpoint temperature."""
    return max(0.5, gal * 8.34 * max(10, setpoint_f - inlet_f) / 3412.14)


def json_path(obj, path):
    for part in filter(None, path.split(".")):
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    return float(obj)


class Tracker:
    """Heater state machine + tank energy balance for one water heater."""

    def __init__(self, source, max_gap):
        self.source, self.max_gap = source, max_gap
        self.on = False
        self.kind = None
        self.since = None  # cycle start (when on) or end of the last cycle (when off)
        self.watts = 0.0
        self.e_kwh = self.th_kwh = 0.0
        self.kind_secs = {}
        self.deficit = 0.0  # kWh of heat the tank is short of setpoint
        self.last_t = None

    def update(self, t, watts, cfg, cap, learned):
        """Feed one reading (W; average since the previous one for the cloud source). Returns a finished cycle or None."""
        done = None
        dt = 0 if self.last_t is None else t - self.last_t
        if dt > self.max_gap:  # monitoring was interrupted: close any cycle where we last saw it
            done = self._finish(self.last_t) if self.on else None
            dt = 0
        self.last_t, self.watts = t, watts
        heating = watts >= cfg["on_w"]
        kind = "element" if watts >= cfg["element_w"] else "heat_pump"
        if heating:
            if not self.on:
                self.on, self.since = True, t - dt
                self.e_kwh = self.th_kwh = 0.0
                self.kind_secs = {}
                self.deficit = max(self.deficit, learned)
            th_kwh = watts * (1 if kind == "element" else cfg["cop"]) * dt / 3.6e6
            self.e_kwh += watts * dt / 3.6e6
            self.th_kwh += th_kwh
            self.kind_secs[kind] = self.kind_secs.get(kind, 0) + dt
            self.kind = kind
            self.deficit = max(MIN_FLOOR * cap, self.deficit - th_kwh)
        else:
            if self.on:
                done = self._finish(t - dt)
            standby_kw = cfg["tank_gal"] * 8.34 * STANDBY_F_PER_H / 3412.14
            self.deficit = min(cap, self.deficit + standby_kw * dt / 3600)
        return done

    def _finish(self, end):
        c = dict(s=round(self.since), e=round(end), kwh=round(self.e_kwh, 3), th=round(self.th_kwh, 3), src=self.source,
                 kind=max(self.kind_secs, key=self.kind_secs.get) if self.kind_secs else self.kind or "heat_pump")
        self.on, self.since, self.deficit = False, end, 0.0
        return c

    def level(self, cap):
        return max(0, min(100, round(100 * (1 - self.deficit / cap))))

    def current(self, cap, learned, now):
        return dict(level=self.level(cap), heating=self.on, kind=self.kind if self.on else None,
                    watts=round(self.watts), since=self.since, source=self.source,
                    cap_kwh=round(cap, 2), learned_kwh=round(learned, 2), updated=self.last_t,
                    cycle_kwh=round(self.e_kwh, 2) if self.on else None)


def learned_cycle(cycles, cap):
    """Typical heat put back per cycle; what a new cycle is assumed to start short by."""
    th = [c["th"] for c in cycles[-20:] if c["e"] - c["s"] >= 120 and c["th"] > 0]
    return min(cap * .9, statistics.median(th)) if len(th) >= 3 else cap * .2


# --------------------------------------------------------------------------- charts
def _split_hours(s, e):
    """Yield (date, hour, minutes) pieces of [s, e)."""
    t, end = datetime.fromtimestamp(s), datetime.fromtimestamp(e)
    while t < end:
        nxt = min(end, t.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        yield t.date(), t.hour, (nxt - t).total_seconds() / 60
        t = nxt


def build_activity(samples, cycles, cur, now):
    """samples: [{t, hw, est}], cycles: [{s, e, kwh, th, kind}] incl. one still running."""
    mins, days, seen = {}, {}, {datetime.fromtimestamp(s["t"]).date() for s in samples}
    for c in cycles:
        d0 = datetime.fromtimestamp(c["s"]).date()
        day = days.setdefault(d0, dict(cycles=0, kwh=0.0, heat_pump=0.0, element=0.0))
        day["cycles"] += 1
        day["kwh"] += c["kwh"]
        for d, h, m in _split_hours(c["s"], c["e"]):
            mins[(d, h, c["kind"])] = mins.get((d, h, c["kind"]), 0) + m
            days.setdefault(d, dict(cycles=0, kwh=0.0, heat_pump=0.0, element=0.0))[c["kind"]] += m / 60
            seen.add(d)
    today = datetime.fromtimestamp(now).date()
    past = [d for d in sorted(seen) if d < today][-TREND_DAYS:]
    daily = []
    for i in range(TREND_DAYS - 1, -1, -1):
        d = today - timedelta(days=i)
        x = days.get(d, dict(cycles=0, kwh=0.0, heat_pump=0.0, element=0.0)) if d in seen else None
        daily.append(dict(date=d.isoformat(), **({k: round(v, 3) for k, v in x.items()} if x else
                                                 dict(cycles=None, kwh=None, heat_pump=None, element=None))))
    day0 = now - 86400
    return dict(
        estimate=cur,
        has_data=bool(seen),
        day=dict(start=day0, end=now,
                 samples=[dict(t=s["t"], hw=s.get("hw"), est=s.get("est")) for s in samples if s["t"] >= day0],
                 cycles=[dict(s=max(c["s"], day0), e=c["e"], kind=c["kind"], kwh=c["kwh"], open=c.get("open", False),
                              start=c["s"]) for c in cycles if c["e"] >= day0]),
        today=[{k: round(mins.get((today, h, k), 0), 1) for k in KINDS} for h in range(24)],
        typical=[round(sum(mins.get((d, h, k), 0) for d in past for k in KINDS) / len(past), 1) for h in range(24)] if past else None,
        typical_days=len(past),
        daily=daily)


# --------------------------------------------------------------------------- live monitor
class Monitor:
    def __init__(self, store: Path):
        self.store = store
        self.samples_file = store / "samples.jsonl"
        self.cycles_file = store / "cycles.jsonl"
        self.settings_file = store / "monitor-settings.json"
        self.trackers, self.counters, self.devs = {}, {}, {}
        self.task = None
        try:
            self.settings = json.loads(self.settings_file.read_text())
        except (OSError, ValueError):
            self.settings = {}
        self._prune()

    # ---- settings
    def config(self, jid):
        cfg = dict(DEFAULTS, **self.settings.get(jid, {}))
        if not cfg["tank_gal"]:
            cfg["tank_gal"] = default_tank_gal(self.devs.get(jid, {}).get("model"))
        return cfg

    def public_config(self, jid):
        cfg = self.config(jid)
        cfg["has_token"] = bool(cfg.pop("power_token"))
        return cfg

    def save_config(self, jid, new):
        cur = self.settings.get(jid, {})
        if new.get("power_token") is None:  # not sent -> keep the saved token
            new["power_token"] = cur.get("power_token", "")
        if new.get("power_url") != cur.get("power_url") or new.get("power_path") != cur.get("power_path"):
            self.trackers.pop(jid, None)  # source changed: start a fresh state machine
        self.settings[jid] = {k: new[k] for k in DEFAULTS if new.get(k) is not None}
        self.store.mkdir(parents=True, exist_ok=True)
        tmp = self.settings_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.settings, indent=1))
        tmp.chmod(0o600)  # may hold a Home Assistant token
        tmp.replace(self.settings_file)

    def cap(self, jid):
        cfg = self.config(jid)
        sp = self.devs.get(jid, {}).get("status", {}).get("setpoint") or 120
        return capacity_kwh(cfg["tank_gal"], sp, cfg["inlet_f"])

    # ---- power meter
    async def read_power(self, cfg):
        headers = {"Authorization": f"Bearer {cfg['power_token']}"} if cfg.get("power_token") else {}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as http:
            async with http.get(cfg["power_url"], headers=headers) as r:
                r.raise_for_status()
                data = await r.json(content_type=None)
        w = json_path(data, cfg["power_path"])
        if math.isnan(w):
            raise ValueError("power reading is not a number")
        return abs(w) * (1000 if cfg["power_unit"] == "kW" else 1)

    # ---- storage
    def _append(self, path, rows):
        if rows:
            self.store.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write("".join(json.dumps(r) + "\n" for r in rows))

    def _read(self, path, jid, since):
        out = []
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("j") == jid and r.get("e", r.get("t", 0)) >= since:
                        out.append(r)
        except OSError:
            pass
        return out

    def _prune(self):
        def when(line):
            try:
                r = json.loads(line)
                return r.get("e", r.get("t", 0))
            except ValueError:
                return 0
        cut = time.time() - KEEP_DAYS * 86400
        for path in (self.samples_file, self.cycles_file):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            keep = [l for l in lines if when(l) >= cut]
            if len(keep) < len(lines):
                path.write_text("".join(l + "\n" for l in keep), encoding="utf-8")

    def cycles(self, jid, since=0):
        return self._read(self.cycles_file, jid, since)

    def learned(self, jid):
        return learned_cycle(self.cycles(jid, time.time() - 14 * 86400), self.cap(jid))

    # ---- sampling loop
    def start(self, get_devices, get_energy):
        self.stop()
        self.task = asyncio.create_task(self._run(get_devices, get_energy))

    def stop(self):
        if self.task:
            self.task.cancel()
            self.task = None

    def _tracker(self, jid, source):
        tr = self.trackers.get(jid)
        if not tr or tr.source != source:
            tr = self.trackers[jid] = Tracker(source, POWER_SECS * 4 if source == "meter" else SAMPLE_SECS * 3)
        return tr

    def _feed(self, jid, tr, t, watts):
        cfg = self.config(jid)
        done = tr.update(t, watts, cfg, self.cap(jid), self.learned(jid))
        if done:
            self._append(self.cycles_file, [dict(done, j=jid)])

    async def _run(self, get_devices, get_energy):
        next_cloud, last_prune = 0.0, time.time()
        while True:
            now = time.time()
            if now >= next_cloud:
                next_cloud = now + SAMPLE_SECS
                try:
                    await self._cloud_tick(get_devices, get_energy, now)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # keep sampling through transient cloud errors
                    log.warning("Water heater sample failed: %r", e)
            for jid in list(self.devs):
                cfg = self.config(jid)
                if not cfg["power_url"]:
                    continue
                try:
                    w = await self.read_power(cfg)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("Power meter read failed for %s: %r", jid, e)
                    continue
                self._feed(jid, self._tracker(jid, "meter"), time.time(), w)
            if now - last_prune > 86400:
                last_prune = now
                self._prune()
            await asyncio.sleep(max(1.0, min(next_cloud, time.time() + POWER_SECS) - time.time()))

    async def _cloud_tick(self, get_devices, get_energy, now):
        self.devs = {d["junction_id"]: d for d in await get_devices()}
        rows = []
        for jid, d in self.devs.items():
            if not self.config(jid)["power_url"]:
                await self._counter_tick(jid, get_energy, now)
            tr = self.trackers.get(jid)
            rows.append(dict(t=round(now), j=jid, hw=d["status"]["hot_water"],
                             est=tr.level(self.cap(jid)) if tr else None,
                             on=bool(tr and tr.on), w=round(tr.watts) if tr else None))
        self._append(self.samples_file, rows)

    async def _counter_tick(self, jid, get_energy, now):
        """Cloud source: turn the rise in today's kWh counter into an average power for the interval."""
        hist = (await get_energy(jid)).get("history") or []
        if not hist:
            return
        day, kwh = str(hist[-1]["date"]), float(hist[-1]["kwh"])
        prev = self.counters.get(jid)
        self.counters[jid] = (day, kwh, now)
        if not prev:
            return
        delta = kwh - prev[1] if day == prev[0] else kwh
        dt = now - prev[2]
        w = max(0.0, delta) * 3.6e6 / dt if dt > 0 else 0
        if w > 6000:  # more than the heater can draw: the cloud caught up in a batch, so the timing is unknown
            return
        self._feed(jid, self._tracker(jid, "cloud"), now, w)

    # ---- API payload
    def activity(self, jid):
        now = time.time()
        since = now - (TREND_DAYS + 1) * 86400
        cycles = self.cycles(jid, since)
        tr = self.trackers.get(jid)
        cap, learned = self.cap(jid), learned_cycle(cycles, self.cap(jid))
        if tr and tr.on:
            cycles.append(dict(s=round(tr.since), e=round(now), kwh=round(tr.e_kwh, 3), th=round(tr.th_kwh, 3),
                               kind=tr.kind, open=True))
        cur = tr.current(cap, learned, now) if tr else dict(level=None, heating=False, source="none")
        samples = self._read(self.samples_file, jid, since)
        if tr:  # end the comparison lines at "now"
            hw = self.devs.get(jid, {}).get("status", {}).get("hot_water")
            samples.append(dict(t=round(now), hw=hw, est=tr.level(cap)))
        return build_activity(samples, cycles, cur, now)


# --------------------------------------------------------------------------- demo
_demo_cache = {}


def demo_activity(jid, model):
    """31 days of a simulated tank, run through the real Tracker, so demo mode shows realistic charts."""
    now = time.time()
    hit = _demo_cache.get(jid)
    if hit and now - hit[0] < 60:
        return hit[1]
    rnd = random.Random(jid)
    element_only = "hp" not in jid
    cfg = dict(DEFAULTS, tank_gal=default_tank_gal(model))
    cap = capacity_kwh(cfg["tank_gal"], 120, cfg["inlet_f"])
    standby_kw = cfg["tank_gal"] * 8.34 * STANDBY_F_PER_H / 3412.14
    tr = Tracker("meter", 600)
    samples, cycles = [], []
    true_def, mode, step = 0.0, None, 60
    lagged = [100.0] * 4  # 20 minutes behind
    start = int(now // step * step) - 31 * 86400
    draws = {}
    for t in range(start, int(now), step):
        dt_ = datetime.fromtimestamp(t)
        day = dt_.date()
        if day not in draws:  # (minute of day, kWh drawn over 10 minutes)
            plan = [(390, 1.9), (430, 1.2), (750, .3), (1110, .8), (1200, 1.5), (1290, .9)]
            draws[day] = [(m + rnd.randint(-30, 30), k * rnd.uniform(.5, 1.4)) for m, k in plan if rnd.random() < .9]
            cop_today = rnd.uniform(2.6, 3.4)
        mod = dt_.hour * 60 + dt_.minute
        for m, k in draws[day]:
            if m <= mod < m + 10:
                true_def += k / 10
        true_def += standby_kw * step / 3600
        if element_only:
            if true_def > .12 * cap:
                mode = "element"
        else:
            if true_def > .55 * cap:
                mode = "element"
            elif true_def > .18 * cap and mode is None:
                mode = "heat_pump"
            elif mode == "element" and true_def < .3 * cap:
                mode = "heat_pump"
        watts = 0.0
        if mode:
            watts = 4500.0 if mode == "element" else rnd.uniform(380, 470)
            true_def -= watts * (1 if mode == "element" else cop_today) * step / 3.6e6
            if true_def <= 0:
                true_def, mode = 0.0, None
        true_def = min(cap, true_def)
        done = tr.update(t, watts, cfg, cap, learned_cycle(cycles, cap))
        if done:
            cycles.append(done)
        if t % SAMPLE_SECS == 0:
            true_lvl = 100 * (1 - true_def / cap)
            lagged.append(true_lvl)
            hw = int(min(100, math.ceil(lagged.pop(0) / 20) * 20))  # the heater's own reading: coarse, late and optimistic
            samples.append(dict(t=t, hw=hw, est=tr.level(cap), true=round(true_lvl)))
    all_cycles = list(cycles)
    if tr.on:
        all_cycles.append(dict(s=round(tr.since), e=round(now), kwh=round(tr.e_kwh, 3), th=round(tr.th_kwh, 3),
                               kind=tr.kind, open=True))
    out = build_activity(samples, all_cycles, tr.current(cap, learned_cycle(cycles, cap), now), now)
    out["demo_hw"] = samples[-1]["hw"] if samples else None
    _demo_cache[jid] = (now, out)
    return out
