# Run from the repo root: python tests/test_eg4.py   (see tests/README.md)
import sys, tempfile
from pathlib import Path
REPO = str(Path(__file__).resolve().parent.parent)
DATA = tempfile.mkdtemp()
import sys, asyncio
sys.path.insert(0, REPO)
from aiohttp import web
import solar as sol
state = {"logins": 0, "expire_next": True}
async def login(req):
    f = await req.post(); state["logins"] += 1
    if f["password"] != "pw": return web.json_response({"success": False, "msg": "account or password error"})
    return web.json_response({"success": True, "plants": [{"name": "Home", "plantId": 1, "inverters": [{"serialNum": "4512345678", "powerRating": 6}]}]})
async def runtime(req):
    if state["expire_next"]:
        state["expire_next"] = False
        return web.Response(text="<html>login</html>", content_type="text/html")
    f = await req.post()
    return web.json_response({"success": True, "serialNum": f["serialNum"], "ppv": 5200, "soc": 99, "pCharge": 300, "pDisCharge": 0, "pinv": 0, "peps": 1100, "pToUser": 0, "pToGrid": 0, "statusText": "PV,Charge"})
async def main():
    app = web.Application(); app.router.add_post("/WManage/api/login", login); app.router.add_post("/WManage/api/inverter/getInverterRuntime", runtime)
    run = web.AppRunner(app); await run.setup(); site = web.TCPSite(run, "127.0.0.1", 8811); await site.start()
    c = sol.EG4Client("me", "pw", "http://127.0.0.1:8811")
    print("inverters", await c.login())
    print("runtime after expiry", sol.normalize(await c.runtime("4512345678")), "logins", state["logins"])
    await c.close()
    bad = sol.EG4Client("me", "nope", "http://127.0.0.1:8811")
    try: await bad.login()
    except sol.EG4AuthError as e: print("bad password ->", e)
    await bad.close(); await run.cleanup()
asyncio.run(main())
