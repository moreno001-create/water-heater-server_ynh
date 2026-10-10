# Run from the repo root: python tests/test_srv.py   (see tests/README.md)
import sys, tempfile
from pathlib import Path
REPO = str(Path(__file__).resolve().parent.parent)
DATA = tempfile.mkdtemp()
import os, sys, asyncio
os.environ["WH_DATA_DIR"] = DATA
sys.path.insert(0, REPO)
import server
from fastapi.testclient import TestClient
c = TestClient(server.app, base_url="http://wh.example.org")
print("session", c.get("/api/session").json())
print("demo login", c.post("/api/login", json={"demo": True, "remember": True}).json())
print("creds after demo:", server.CRED_FILE.exists())
print("devices", [d["name"] for d in c.get("/api/devices").json()])
print("setpoint same origin", c.post("/api/devices/demo-hp/setpoint", json={"setpoint": 118}, headers={"origin": "https://wh.example.org"}).status_code)
print("setpoint evil origin", c.post("/api/devices/demo-hp/setpoint", json={"setpoint": 118}, headers={"origin": "https://evil.com"}).status_code)
server.save_creds("a@b.c", "pw"); print("perm", oct(os.stat(server.CRED_FILE).st_mode), server.load_creds())
print("logout", c.post("/api/logout").json(), "creds:", server.CRED_FILE.exists())
# saved-login path with a fake live backend
class Fake:
    kind = "live"
    def __init__(self, e, p): self.e = e
    async def devices(self): return []
    async def close(self): pass
server.LiveBackend = Fake; server.save_creds("a@b.c", "pw")
print("auto relogin", c.get("/api/session").json())
print("index", c.get("/").status_code)
