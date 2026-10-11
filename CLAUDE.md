# Handoff notes for Claude (or any developer)

Read this first, then `README.md` (user-facing: what it does, how to install). This file records how the code is put together,
why it was built this way, what's verified and what's still open.

> **Latest status (2026-10-10):**
> - **Installed and working** on the owner's YunoHost server, through the web admin from the public copy `water-heater-server_ynh`.
> - **The active work is in the sibling repo `moreno001-create/eg4-dashboard`.** Read the "START HERE" section of its `CLAUDE.md`: restyle it to look like the
>   owner's **Voltline** iPad app (the source is probably on the Mac), plus the pending decisions.
> - **Solar server source (2026-10-10, built on the Mac):** the dump load now reads the inverter from eg4-dashboard's `/api/live` first. eg4-dashboard reads the
>   Wi-Fi dongle directly every 5 s. EG4 Monitor is the fallback. See "Where readings come from" below.
>   - Needs eg4-dashboard **installed on the same YunoHost server** (then the address is `http://127.0.0.1:8766`). Until then, leave the address empty.
> - **Still open here:** a light/dark/auto theme switch like the one in eg4-dashboard (offered, not answered yet).

## What this is
A self-hosted dashboard for the owner's **A. O. Smith iCOMM heat-pump water heater** and **EG4 6000XP** solar inverter,
packaged as a **YunoHost** app (the owner's home server). Single household, one iCOMM account, one EG4 account.
It started as the web server from the owner's `moreno001-create/water-heater` repo (which also has Android/iOS apps that embed
the same `index.html`) and became its own repo.

The owner's setup and choices (from conversation):
- The water heater is wired to the **6000XP's output**, so the ~4.5 kW element counts against the inverter's 6 kW rating.
- **No power meter yet** on the heater circuit (may add one, e.g. a Shelly EM). The cycle monitor falls back to the iCOMM kWh counter until then.
- Reads the inverter from the owner's **solar server** (eg4-dashboard, which reads the Wi-Fi dongle on the home network), falling back to the **EG4 Monitor cloud**.
- First chose mode-only dumping, then asked for the **optional setpoint boost**, which is now built (off by default).

## Layout
| File | Role |
|---|---|
| `server.py` | FastAPI app: iCOMM backends (live via py-aosmith, `DemoBackend`), session/login, REST API, wires up `monitor` and `solar` |
| `monitor.py` | Heating-cycle detection (`Tracker` state machine) + tank energy-balance estimate, history files, demo tank simulation |
| `solar.py` | Minimal EG4 cloud client (`EG4Client`), `Solar` poll loop + dump-load rules, demo sunny-day simulation |
| `index.html` | The whole UI (vanilla JS, inline SVG charts, no build step). Two tabs: Water heater / Solar |
| `manifest.toml`, `scripts/`, `conf/`, `doc/` | YunoHost package (packaging v2, helpers 2.1, YunoHost >= 12) |
| `tests/` | Script-style tests with fakes; see `tests/README.md` |

## How it works
- **One process, background loops.** `MON` (monitor.Monitor) runs while a *live* iCOMM backend is signed in (`use_backend()`).
  `SOLAR` (solar.Solar) starts at app startup (lifespan) and polls EG4 every 60 s. In hosted mode the lifespan also re-logs into iCOMM
  from the saved credentials, so monitoring resumes after a reboot without anyone opening the page.
- **Config by env vars** (`WH_HOST`, `WH_PORT`, `WH_DATA_DIR`). `WH_DATA_DIR` set means hosted. It enables saving the iCOMM login
  (`credentials.json`, mode 600) and lets the origin check trust the reverse proxy's `Host` header. Locally (no `WH_DATA_DIR`) data files go next to `server.py`.
- **API** (all JSON, under `/api`): `session`, `login`, `logout`, `devices`, `devices/{jid}`, `.../energy`, `.../setpoint`, `.../mode`,
  `.../activity` (estimate + charts), `.../monitor` (GET/POST cycle-monitor settings), `.../monitor/test`; `solar` (GET),
  `solar/control`, `solar/settings`, `solar/connect`. The UI uses **relative** paths (`api/...`) so it works under a sub-path like `/waterheater/`.
- **Native apps**: `index.html` still contains the Android/iOS JS bridge code (`NATIVE`). The phone apps implement only the base API,
  so the UI hides the activity cards and the Solar tab when those endpoints 404. Keep that degradation working.
- **Data files** (in `WH_DATA_DIR` on YunoHost = `/home/yunohost.app/waterheater`): `credentials.json`, `monitor-settings.json`,
  `samples.jsonl` + `cycles.jsonl` (60 days), `solar-settings.json` (EG4 password, mode 600), `solar-state.json` (dump state,
  survives restarts), `solar.jsonl` (60 days). All are git-ignored.

### Cycle monitor (`monitor.py`)
- The iCOMM API exposes **no on/off state and no tank temperature** for heat pumps, only `hotWaterStatus` and daily kWh. That's why the
  monitor exists: the owner doesn't trust the heater's hot-water reading.
- Sources: a **power meter** (any JSON HTTP API; URL + dotted field path + optional bearer token; polled every 30 s), or the **iCOMM
  kWh counter** (polled every 5 min; a rise means "on"). Counter jumps above 6 kW-equivalent are treated as batch catch-ups and ignored.
- `Tracker`: on/off from a watts threshold; heat pump vs element from a second threshold. Heat delivered = W x COP (heat pump) or x1 (element).
  Estimate = tank capacity (gal x 8.34 x (setpoint - inlet) / 3412) minus a deficit. At cycle start the deficit = the learned
  median heat per cycle; it shrinks while heating; it's 0 when the heater stops; standby loss adds between cycles.
- Demo mode runs 31 simulated days through the real `Tracker`, so the charts exercise real code.

### Solar dump load (`solar.py`)
- `EG4Client` mirrors **pylxpweb** (MIT, github.com/joyfulhouse/pylxpweb): `POST /WManage/api/login` (form: account, password,
  language=ENGLISH; the response lists `plants[].inverters[].serialNum`), then `POST /WManage/api/inverter/getInverterRuntime` (form: serialNum).
  Fields used: `ppv` (or ppv1+2+3), `soc`, `pCharge`, `pDisCharge`, `pinv`/`peps` (inverter output = max of the two), `pToUser`, `pToGrid`, `statusText`.
  Powers are in watts, unscaled. An expired session returns HTML or 401, so it logs in again once. **Why not use pylxpweb directly:** it needs
  Python >= 3.12 and YunoHost 12 (Debian 12) has 3.11.
- Rules (defaults in `solar.DEFAULTS`):
  - **Start** after 3 good readings in a row: SOC >= 98, PV >= 1500 W, battery not discharging, and for Electric, house load + 4500 W under the cut-off.
  - **Stop**: SOC < 90, discharge > 300 W for 2 readings, 3 read failures, or switched off.
  - **Inverter cut-off** (90% of 6000 W) while dumping: stop, then turn the dump load **off** (or pause it).
  - **Manual changes win**: a mode or setpoint changed by hand during a dump is left alone. Vacation mode is never touched. At least 10 minutes between switches.
- Restore order on stop: setpoint first (`_unboost`), then mode. The previous mode, Hot Water+ level and setpoint are saved in `solar-state.json` *before* switching.
  - Two bugs here were caught by tests: the code read the "previous" value after the switch had mutated the same dict. **Always capture before calling a setter.**
- Hot Water+: `solar_set_mode` in server.py passes the level only when the target mode supports it. py-aosmith raises otherwise.

### Where readings come from (added 2026-10-10)
- **Solar server first.** Setting `solar_url` (Solar settings → "Solar server address", e.g. `http://127.0.0.1:8766`) makes `Solar.read()` fetch
  `GET {solar_url}/api/live` from eg4-dashboard. That gives the latest reading with `source` = local (Wi-Fi dongle) / cloud, plus `age` and `stale`.
  - On YunoHost the eg4solar service listens on 127.0.0.1, so this goes app to app, without the YunoHost login.
  - Only eg4-dashboard talks to the dongle: it allows a single local client, which is why the water heater doesn't connect to it itself.
- **Fallback:** if the solar server doesn't answer, answers non-200, or its reading is stale or older than 180 s, the EG4 cloud login (if saved) is read instead.
  - `server_error` says why, and the Solar tab shows it.
  - It goes back to the solar server as soon as it answers.
- **Timing:**
  - While the solar server has live Wi-Fi readings (`fast`), `Solar` polls every 5 s (`FAST_SECS`).
  - Only `guard()` runs on the in-between readings: the inverter cut-off, so a surge stops dumping within seconds.
  - The full rules and the history row still run once a minute (`last_full`), so the start/stop streaks keep their meaning (3 / 2 minutes).
- **Note on the 6000XP:** `inv` (= max(pinv, peps)) reads **0 while the inverter passes grid power through** (seen live 2026-10-10: home 1.5 kW, inv 0).
  The cut-off rule was left as it was. It uses the same figure as before, so it behaves the same from either source.
- Test: `tests/test_source.py`.

## Status
- **Verified locally:** all `tests/` scripts pass; demo mode renders in Edge (light and dark, desktop), screenshots checked.
- **Not yet verified:**
  - **The YunoHost install itself.** The package has never run on a real server. If it fails, likely spots: the helpers 2.1 names (`ynh_config_add_*`,
    `ynh_systemctl --wait_until="Uvicorn running"`), `ynh_restore` usage, `ProtectSystem=full` in the systemd unit.
  - The real iCOMM account (only the base dashboard has been used live).
  - The real EG4 account: field values vs the EG4 app, and how fresh the cloud data is.
  - Whether the iCOMM kWh counter updates during the day.
  - Phone-width layout of the new cards (the headless screenshots at 390 px were unreliable).

## Open ideas / next steps (owner's wishes first)
1. ~~Direct local connection to the 6000XP~~: done through eg4-dashboard (dongle port 8000 works). This app reads it via the solar server, above.
2. A power meter on the heater circuit (the code supports it already; the owner just needs hardware).
3. Possibly a 30-day solar/dump history chart (hours dumped per day, estimated kWh absorbed).

## Conventions
- Keep the UI dependency-free (no framework, no CDN). Charts are hand-rolled SVG: `barChart`, `cmpChart`, `timeChart`.
  - Colors are CSS tokens (`--sHw`, `--sEst`, `--sHp`, `--sEl`, `--sSun`), checked for colorblind safety in light and dark mode.
  - One y-axis per chart, a legend for 2+ series, hover/touch tooltips.
- Plain, friendly UI text. Warn above 120°F (scald), and require the confirm dialog for any setpoint above that.
- When packaging, any new `.py` file must be added to the `cp` list in `scripts/_common.sh`. Bump `version` in `manifest.toml`.
- Commits end with a `Co-Authored-By:` line.
- **Two repos.** `moreno001-create/water-heater-server` is **private** and has the full history; work there. `moreno001-create/water-heater-server_ynh`
  is a **public** copy that exists only so the YunoHost web admin can install it (the web admin needs a public repo whose name ends in `_ynh`).
  The public repo gets snapshot commits authored with the owner's GitHub noreply address, so their personal email stays private.
  To publish the current `main` there, after pushing to the private repo:
  ```
  git fetch public
  git push public "$(GIT_AUTHOR_NAME='Anthony Moreno' GIT_AUTHOR_EMAIL='337951492+moreno001-create@users.noreply.github.com'     GIT_COMMITTER_NAME='Anthony Moreno' GIT_COMMITTER_EMAIL='337951492+moreno001-create@users.noreply.github.com'     git commit-tree HEAD^{tree} -p public/main -m "Update to $(git rev-parse --short HEAD): <summary>")":refs/heads/main
  ```
  (Remote `public` = https://github.com/moreno001-create/water-heater-server_ynh.git. Never put secrets in the repo; the public copy must stay clean.)
