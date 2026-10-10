# Tests

Script-style checks: each one prints what it saw, and you compare the output with the expected behaviour noted below.
They use fakes (a simulated heater, a fake EG4 server, scripted inverter readings), so they need no accounts or hardware.

```
pip install -r requirements.txt httpx2     # httpx2: needed by FastAPI's TestClient
python tests/test_srv.py
```

| Script | Checks | Expect |
|---|---|---|
| `test_srv.py` | Hosted mode: demo login, saved iCOMM login (save, delete on sign-out, reconnect), cross-origin block | `setpoint evil origin 403`, `auto relogin ... connected: True` |
| `test_mon.py` | Cycle monitor: settings API (token never returned), power-meter cycles, kWh-counter cycles, batch-update rejection | `heat_pump` 90 min + `element` 10 min cycles; the 5 kWh jump is ignored |
| `test_solar.py` | Dump-load rules with a fake heater: start after 3 readings, inverter limit (stop + turn off), battery discharge, manual override, headroom | Heater restored to `heat_pump` each time; last event "switched to hybrid by hand" |
| `test_boost.py` | Setpoint boost: raise/restore, a setpoint changed by hand is left alone, boost off mid-dump, capped at the heater's max | `('sp', 135)` then `('sp', 120)`; capped run shows `('sp', 130)` |
| `test_eg4.py` | EG4 client against a fake EG4 server on port 8811: login, inverter discovery, re-login after expiry, bad password | `logins 2`, `bad password -> EG4: account or password error` |

On Windows, set `PYTHONIOENCODING=utf-8` so the ° sign prints correctly.
