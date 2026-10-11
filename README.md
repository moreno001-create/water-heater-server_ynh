# Water Heater Server

Self-hosted web dashboard for A. O. Smith iCOMM water heaters, built on the unofficial
[py-aosmith](https://github.com/bdr99/py-aosmith) API. It may stop working without notice, and is not affiliated with A. O. Smith.

This is the web server from the [water-heater](https://github.com/moreno001-create/water-heater) project (which also has
the Android and iOS apps), packaged as a [YunoHost](https://yunohost.org) app so you can reach it from anywhere behind YunoHost's login.

- Shows hot water availability, setpoint, mode and energy use
- Change setpoint (warns above 120 F) and mode, including vacation
- Pick which water heater when you have several
- Second gauge that estimates the hot water from the heater's on/off cycles, to compare with the heater's own reading
- Charts: heater's reading vs estimate with on/off cycles (24 h), heating per hour today vs a typical day, and heating
  cycles per day (30 days), split between heat pump and element
- Solar tab for an EG4 inverter (e.g. 6000XP): solar, battery, inverter output and grid, with 24-hour charts
- Solar dump load: when the batteries are full and solar is plentiful, switch the water heater to Electric or Hybrid mode
  (your choice, on/off switch), optionally raising the setpoint, and back again; stops and turns itself off if the inverter nears its limit
- Demo mode with simulated heaters, no account needed

## How it works (overview)
```
                    YunoHost (your home server)
 browser ──https──► nginx + YunoHost login ──► waterheater service (Python, 127.0.0.1)
                                                 │
                     ┌───────────────────────────┼─────────────────────────────┐
                     ▼                           ▼                             ▼
            A. O. Smith iCOMM cloud     power meter (optional,         EG4 Monitor cloud
            (read status, change        on your network)               (solar, battery,
             mode / setpoint)                                           inverter output)
```
- **One small server does everything.** It serves the page, talks to the A. O. Smith and EG4 clouds, and runs two background jobs
  while it is signed in: the **cycle monitor** (works out when the heater is heating) and the **solar dump load** (decides when to
  send surplus solar into the water heater).
- **The page** has two tabs. *Water heater* has the gauges, setpoint, mode, energy, and the cycle charts. *Solar* has the inverter, battery, and dump-load controls.
- **It keeps working without the page open.** On YunoHost it signs back in by itself after a restart and keeps 60 days of history.
- **Everything you set is saved on the server.** That includes the iCOMM login (if you tick "Stay signed in"), the EG4 login, and the monitor and dump-load settings.
  The files are in the app's data folder and readable only by the app.

The sections below explain each part. `CLAUDE.md` has the technical notes for whoever works on the code next.

## Layout
| Path | What it is |
|---|---|
| `server.py`, `index.html`, `requirements.txt` | The web app (FastAPI server + UI) |
| `monitor.py` | Heating-cycle monitor and hot-water estimate |
| `solar.py` | EG4 inverter monitor and solar dump-load controller |
| `manifest.toml`, `scripts/`, `conf/`, `doc/` | YunoHost package (packaging v2, helpers 2.1, YunoHost 12+) |
| `tests/` | Checks that run with fakes, no accounts needed (see `tests/README.md`) |
| `CLAUDE.md` | Handoff notes: design, decisions, status and next steps |

## Install on YunoHost
**From the web admin (easiest):** go to *Applications → Install*, scroll to **Install custom app**, and paste
`https://github.com/moreno001-create/water-heater-server_ynh` (the public copy of this app). Accept the third-party
warning, pick the domain, keep the path `/waterheater`, choose who may use it (not visitors), and click **Install**.

**From a terminal on the server:** the main repo is private, so the server needs a GitHub token to download it. On GitHub, go to Settings → Developer settings →
Personal access tokens → Fine-grained tokens. Give the token access to only this repository, with **Contents: Read-only**. Then, on the server:
```
git clone https://github.com/moreno001-create/water-heater-server.git    # username + the token as password
sudo yunohost app install ./water-heater-server
```
You are asked for the domain, the path (default `/waterheater`) and which group may use it (default `admins`).
Don't give access to visitors: anyone who can open the page can change the water heater.

Open `https://<your domain>/waterheater`, sign in to YunoHost, then sign in with your iCOMM account. Tick
"Stay signed in on this server" so it reconnects by itself after a reboot. For solar, open the Solar tab → Solar settings,
enter your EG4 Monitor login, click **Connect & find inverter**, then **Save**. The dump load starts switched off.

Upgrade after pulling new commits:
```
cd water-heater-server && git pull
sudo yunohost app upgrade waterheater -f ./ --force
```
Logs: `/var/log/waterheater/waterheater.log` or `sudo yunohost service log waterheater`.

## How it works when hosted
- nginx forwards `https://<domain>/<path>/` to the app on `127.0.0.1:<port>`. YunoHost SSO allows only the chosen group in.
- The systemd service runs as its own `waterheater` user in a Python virtualenv under `/var/www/waterheater`.
- If you tick "Stay signed in", your iCOMM login is saved to `/home/yunohost.app/waterheater/credentials.json`
  (mode 600, readable only by the app user). Signing out deletes it. YunoHost backups include it.
- There is one iCOMM connection, shared by everyone who can open the page.

## Heating cycles and the estimated gauge
The iCOMM cloud reports a hot-water level but not whether the heater is running. While the server is signed in (it signs
back in on its own after a restart if you ticked "Stay signed in"), it watches for heating cycles from one of two sources:

- **A power meter on the heater's circuit (recommended).** Read every 30 seconds, so on/off times are accurate. It also tells
  the heat pump (a few hundred W) from the elements (~4,500 W). Set it up under *Cycle monitor settings* on the page: any device
  with a JSON HTTP API works. Examples are listed there: Shelly Pro EM / EM Gen3 / Pro 3EM / EM, a Home Assistant sensor, or Tasmota.
  The server has to be able to reach the meter on your network.
- **The iCOMM kWh counter (fallback).** Read every 5 minutes. A 5-minute slot counts as heating when the counter went up.
  This is coarse, and it depends on how often A. O. Smith updates the counter during the day.

The estimate is an energy balance of the tank. Its capacity comes from the tank size, setpoint and inlet temperature. Each
heating cycle is assumed to start with the tank short by a typical cycle's worth of heat (the median of recent cycles). The
level climbs back at the heater's output (power x COP for the heat pump, x1 for the elements) and reads full when the heater
stops. Between cycles it drops slowly with standby loss. Hot water used between cycles only shows once the heater turns on,
so it's an estimate. Its strength is that it comes from a different source than the heater's own reading.

History is kept for 60 days in `samples.jsonl` and `cycles.jsonl` (in `/home/yunohost.app/waterheater` on YunoHost, next to
`server.py` when run locally).

## Solar dump load (EG4 inverter)
The Solar tab reads the inverter from your **solar server** first: the EG4 solar dashboard (`eg4-dashboard`), which reads the
inverter's Wi-Fi dongle on your home network every few seconds. Install it on the same YunoHost server and put
`http://127.0.0.1:8766` in *Solar settings → Solar server address*. While it has live Wi-Fi readings, the inverter limit below is
checked every 5 seconds.

When the solar server isn't set or can't be reached, it reads the **EG4 Monitor cloud** instead, the same data the EG4 app shows,
about once a minute. Connect that under *Solar settings* with your EG4 Monitor email and password. The password
is saved in `solar-settings.json` in the data folder (mode 600). The EG4 calls follow
[pylxpweb](https://github.com/joyfulhouse/pylxpweb), which isn't used directly because it needs Python 3.12 and YunoHost 12 has 3.11.

With the dump load switched on, the server signed in to iCOMM, and **Electric** or **Hybrid** chosen:

- **Start**: when for 3 readings in a row the battery is at or above the start level (98%), solar is above the minimum
  (1,500 W), the battery isn't discharging, and (for Electric) the house load plus ~4,500 W stays under the cut-off. The
  heater is switched to the dump mode; its previous mode and Hot Water+ level are remembered (even across restarts).
- **Stop**: when the battery falls below the stop level (90%), or discharges more than 300 W for 2 readings (solar no longer
  covers it), or the inverter can't be read 3 times. The heater goes back to its previous mode. At least 10 minutes pass
  between switches.
- **Inverter limit**: if the inverter output reaches the cut-off (90% of 6,000 W by default) while dumping, the heater is switched
  back right away and the dump load is **turned off** (or paused for 30 minutes, if you choose). You turn it back on.
- **Manual changes win**: if you change the heater's mode while it is dumping, the dump load steps aside and doesn't switch it back.
- **Setpoint boost (optional)**: also raise the setpoint (e.g. to 130-140°F, capped at the heater's maximum) while dumping, so
  a full tank keeps absorbing solar; the original setpoint is put back first when dumping stops, unless you changed it by
  hand meanwhile. Above 120°F, fit a thermostatic mixing valve at the heater so the taps can't scald.
- Vacation mode is never overridden.

With a 6 kW inverter and a ~4.5 kW element, Electric-mode dumping only fits when the rest of the house uses under ~900 W.
Hybrid absorbs less (mostly the heat pump) but rarely gets near the limit. Readings are seconds old from the solar server and up
to a minute old from the cloud. Either way this supplements the inverter's own overload protection; it doesn't replace it.

## Run locally (Windows/Mac/Linux)
```
pip install -r requirements.txt
python server.py        # opens http://127.0.0.1:8765
```
Run locally, your iCOMM password is kept in memory only. The EG4 login (if you set up Solar) is saved in
`solar-settings.json` next to `server.py`. Environment variables: `WH_HOST`, `WH_PORT`, and `WH_DATA_DIR`
(enables "Stay signed in" and reverse-proxy mode). See the top of `server.py`.
