"""Mock-Backend fuer die W-BOT-UI (Vertrag: docs/ARCHITECTURE.md). Keine echte Hardware.

Start:  .venv\\Scripts\\python.exe ui\\dev\\mock_server.py      ->  http://127.0.0.1:8123/
Umgebung: MOCK_SPEED (Standard 3) beschleunigt die Fahrten, MOCK_PORT aendert den Port.
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402
from fastapi import FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse, PlainTextResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

import mock_patterns as pat  # noqa: E402
from mock_machine import ApiErr, MockMachine  # noqa: E402

mimetypes.add_type("application/javascript", ".js")   # Windows-Registry liefert sonst text/plain
mimetypes.add_type("text/css", ".css")

UI_DIR = Path(__file__).resolve().parents[1]
CFG_PATH = UI_DIR.parent / "config" / "wbot.yaml"


def load_cfg() -> dict:
    if CFG_PATH.exists():
        return yaml.safe_load(CFG_PATH.read_text(encoding="utf-8"))
    raise SystemExit(f"{CFG_PATH} fehlt")


m = MockMachine(load_cfg())


@asynccontextmanager
async def lifespan(_: FastAPI):
    async def pump():
        last_state = last_job = None
        while True:
            await asyncio.sleep(0.1)
            s = json.dumps(m.snapshot(), sort_keys=True)
            if s != last_state:
                last_state = s
                m.emit({"type": "state", "data": json.loads(s)})
            j = json.dumps(m.job_info(), sort_keys=True)
            if j != last_job:
                last_job = j
                m.emit({"type": "job", "data": m.job_info()})
    task = asyncio.create_task(pump())
    yield
    task.cancel()


app = FastAPI(title="W-BOT UI Mock", lifespan=lifespan)


@app.exception_handler(ApiErr)
async def _api_err(_: Request, e: ApiErr):
    return JSONResponse({"error": e.msg, "code": e.code}, status_code=e.status)


@app.exception_handler(RequestValidationError)
async def _val_err(_: Request, e: RequestValidationError):
    return JSONResponse({"error": "Ungültige Eingabe.", "code": "validation"}, status_code=422)


@app.exception_handler(StarletteHTTPException)
async def _http_err(_: Request, e: StarletteHTTPException):
    return JSONResponse({"error": "Nicht gefunden." if e.status_code == 404 else str(e.detail), "code": "http"},
                        status_code=e.status_code)


@app.middleware("http")
async def no_cache(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


async def body(req: Request) -> dict:
    try:
        d = await req.json()
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


@app.get("/api/state")
async def state():
    return m.snapshot()


@app.get("/api/ports")
async def ports():
    return [{"device": "COM3", "description": "USB-SERIAL CH340 (COM3)"},
            {"device": "COM7", "description": "Gesperrter Testport (COM7) – liefert einen Fehler"}]


@app.post("/api/connect")
async def connect(req: Request):
    return await m.connect(await body(req))


@app.post("/api/disconnect")
async def disconnect():
    return await m.disconnect()


@app.post("/api/reference")
async def reference(req: Request):
    b = await body(req)
    if "x" not in b or "y" not in b:
        raise ApiErr(400, "X und Y fehlen.", "bad_request")
    return await m.set_reference(float(b["x"]), float(b["y"]))


@app.post("/api/jog")
async def jog(req: Request):
    b = await body(req)
    return await m.jog(float(b.get("dx", 0)), float(b.get("dy", 0)), b.get("feed"))


@app.post("/api/goto")
async def goto(req: Request):
    b = await body(req)
    if "x" not in b or "y" not in b:
        raise ApiErr(400, "X und Y fehlen.", "bad_request")
    return await m.goto(float(b["x"]), float(b["y"]), b.get("feed"), b.get("pen"))


@app.post("/api/park")
async def park():
    return await m.park()


@app.post("/api/pen")
async def pen(req: Request):
    b = await body(req)
    return await m.set_pen(b.get("state"), b.get("angle"))


@app.post("/api/revolver")
async def revolver(req: Request):
    b = await body(req)
    return await m.set_revolver(b.get("slot"), b.get("angle"))


@app.post("/api/servo")
async def servo(req: Request):
    b = await body(req)
    return await m.set_servo_raw(b.get("servo"), float(b.get("angle", 0)))


for _n in ("hold", "resume", "cancel", "estop", "unlock", "reset"):
    def _mk(n):
        async def h():
            return await getattr(m, n)()
        return h
    app.post(f"/api/{_n}")(_mk(_n))


@app.post("/api/gcode")
async def gcode(req: Request):
    return {"replies": await m.gcode((await body(req)).get("line"))}


@app.get("/api/patterns")
async def patterns():
    return pat.PATTERNS


@app.post("/api/preview")
async def preview(req: Request):
    return m.plan(await body(req))


@app.post("/api/jobs")
async def jobs(req: Request):
    return await m.start_job(await body(req))


@app.get("/api/job")
async def job():
    return m.job_info()


@app.post("/api/svg")
async def svg(file: UploadFile = File(...)):
    data = (await file.read()).decode("utf-8", "ignore")
    if "<svg" not in data:
        raise ApiErr(400, "Das ist keine SVG-Datei.", "bad_svg")
    import math  # Mock: statt zu parsen ein Stern
    pts = []
    for i in range(11):
        r = 250 if i % 2 == 0 else 100
        a = math.pi / 2 + i * math.pi / 5
        pts.append([round(750 + r * math.cos(a), 2), round(500 + r * math.sin(a), 2)])
    return {"drawing": {"name": file.filename or "SVG", "strokes": [{"color": 0, "points": pts}]}}


@app.get("/api/config")
async def get_config():
    return m.cfg


@app.put("/api/config")
async def put_config(req: Request):
    return m.update_config(await body(req))


@app.get("/api/firmware-config", response_class=PlainTextResponse)
async def firmware_config():
    return "# MOCK firmware config (nicht fuer die Maschine!)\nname: W-BOT mock\nkinematics:\n  WallPlotter:\n    left_axis: 0\n"


@app.get("/api/simulator")
async def simulator():
    return m.sim_info()


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=2000)
    m.listeners.add(q)
    try:
        await sock.send_json({"type": "state", "data": m.snapshot()})
        for rec in list(m.logs)[-60:]:
            await sock.send_json(rec)
        recv = asyncio.create_task(sock.receive_text())
        while True:
            get = asyncio.create_task(q.get())
            done, _ = await asyncio.wait({get, recv}, return_when=asyncio.FIRST_COMPLETED)
            if recv in done:
                get.cancel()
                recv.result()  # wirft WebSocketDisconnect
                recv = asyncio.create_task(sock.receive_text())
            if get in done:
                await sock.send_json(get.result())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        m.listeners.discard(q)


app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("MOCK_PORT", "8123")), log_level="warning")
