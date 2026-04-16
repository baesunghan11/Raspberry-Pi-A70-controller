"""
FLIR A70 + Arduino 열화상 시스템 — 웹 서버 (Raspberry Pi 5)
HTTP + Radiometric JPEG 방식 (Spinnaker SDK 불필요)
실행: python web_server.py
접속: http://<RPi5 IP>:8000
"""

import asyncio
import base64
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import FileSystemLoader, Environment

from camera import FlirA70Camera
from arduino_reader import ArduinoReader, find_arduino_port

# ─────────────────────────────────────────────
# 앱 초기화
# ─────────────────────────────────────────────
app = FastAPI(title="FLIR Thermal System")
BASE_DIR     = Path(__file__).parent
CAPTURED_DIR = BASE_DIR / "captured"
CAPTURED_DIR.mkdir(exist_ok=True)

_jinja_env = Environment(loader=FileSystemLoader(str(BASE_DIR / "templates"), encoding="utf-8"))
templates = Jinja2Templates(env=_jinja_env)
app.mount("/captured", StaticFiles(directory=str(CAPTURED_DIR)), name="captured")

# ─────────────────────────────────────────────
# 전역 상태
# ─────────────────────────────────────────────
_lock = threading.Lock()

state = {
    "camera":         None,
    "cam_connected":  False,
    "cam_ip":         None,
    "arduino":        None,
    "ard_connected":  False,
    "ard_port":       None,
    "last_capture":   None,
    "auto_running":   False,
    "auto_count":     0,
    "auto_total":     None,
    "auto_interval":  60,
    "_auto_stop":     threading.Event(),
    "stream_running": False,
    "stream_fps":     1.0,
    "stream_frame":   None,
    "stream_stats":   None,
    "_stream_lock":   threading.Lock(),
    "logs":           [],
}

# ─────────────────────────────────────────────
# EMA 컬러맵 안정화
# ─────────────────────────────────────────────
_EMA_ALPHA = 0.05
_ema = {"min": None, "max": None}


def _add_log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    print(entry)
    with _lock:
        state["logs"].append(entry)
        if len(state["logs"]) > 30:
            state["logs"].pop(0)


# ─────────────────────────────────────────────
# 렌더링 헬퍼
# ─────────────────────────────────────────────
def _render_colormap(temp: np.ndarray) -> np.ndarray:
    f_min, f_max = float(temp.min()), float(temp.max())
    if _ema["min"] is None:
        _ema["min"], _ema["max"] = f_min, f_max
    else:
        _ema["min"] = _EMA_ALPHA * f_min + (1 - _EMA_ALPHA) * _ema["min"]
        _ema["max"] = _EMA_ALPHA * f_max + (1 - _EMA_ALPHA) * _ema["max"]
    span = _ema["max"] - _ema["min"] if _ema["max"] > _ema["min"] else 1.0
    norm = np.clip((temp - _ema["min"]) / span * 255, 0, 255).astype(np.uint8)
    cmap = cv2.applyColorMap(norm, cv2.COLORMAP_JET)

    def put(text, y):
        cv2.putText(cmap, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3)
        cv2.putText(cmap, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

    put(f"Min:{temp.min():.1f}C  Max:{temp.max():.1f}C  Avg:{temp.mean():.1f}C", 22)
    return cmap


def _to_b64png(img: np.ndarray) -> str:
    ok, buf = cv2.imencode(".png", img)
    return base64.b64encode(buf.tobytes()).decode() if ok else ""


def _to_jpeg(img: np.ndarray, quality: int = 75) -> "bytes | None":
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else None


# ─────────────────────────────────────────────
# 촬영 공통 로직
# ─────────────────────────────────────────────
def _do_capture(label: str = "단일") -> "dict | None":
    with _lock:
        cam: FlirA70Camera  = state["camera"]
        ard: ArduinoReader  = state["arduino"]
        if cam is None or not state["cam_connected"]:
            _add_log("카메라가 연결되지 않았습니다.")
            return None
        if state["stream_running"]:
            _add_log("스트리밍 중에는 촬영할 수 없습니다.")
            return None

    jpeg_data, temp, w, h, visual = cam.capture_all()
    if jpeg_data is None:
        _add_log("촬영 실패")
        return None

    arduino_data = ard.get_latest() if (ard and ard.connected) else None
    saved = cam.save_capture(jpeg_data, temp, visual, CAPTURED_DIR,
                             arduino_data=arduino_data, prefix="capture")

    cmap_b64   = _to_b64png(_render_colormap(temp))
    visual_b64 = _to_b64png(visual) if visual is not None else ""

    result = {
        "timestamp":   datetime.now().isoformat(timespec="seconds"),
        "width":       w,
        "height":      h,
        "temp_min":    round(float(temp.min()), 2),
        "temp_max":    round(float(temp.max()), 2),
        "temp_mean":   round(float(temp.mean()), 2),
        "temp_median": round(float(np.median(temp)), 2),
        "img_b64":     cmap_b64,
        "visual_b64":  visual_b64,
        "has_visual":  visual is not None,
        "arduino":     arduino_data,
        "files":       saved,
    }
    with _lock:
        state["last_capture"] = result
    _add_log(f"{label} 촬영 완료 | avg={result['temp_mean']:.1f}°C "
             f"({result['temp_min']:.1f}~{result['temp_max']:.1f}°C)")
    return result


# ─────────────────────────────────────────────
# 스트리밍 스레드 (HTTP 폴링 방식)
# ─────────────────────────────────────────────
def _stream_thread(fps: float):
    with _lock:
        cam: FlirA70Camera = state["camera"]
    if cam is None:
        with _lock:
            state["stream_running"] = False
        return

    # EMA 초기화
    _ema["min"] = None
    _ema["max"] = None

    interval = 1.0 / max(fps, 0.1)
    _add_log(f"스트리밍 시작 — {fps:.1f} FPS (HTTP 폴링)")

    try:
        while state["stream_running"]:
            t0 = time.time()

            temp, visual = cam.get_frame()
            if temp is not None:
                jpg = _to_jpeg(_render_colormap(temp))
                if jpg:
                    with state["_stream_lock"]:
                        state["stream_frame"] = jpg
                        state["stream_stats"] = {
                            "temp_min":    round(float(temp.min()), 2),
                            "temp_max":    round(float(temp.max()), 2),
                            "temp_mean":   round(float(temp.mean()), 2),
                            "temp_median": round(float(np.median(temp)), 2),
                        }

            elapsed = time.time() - t0
            remain  = interval - elapsed
            if remain > 0:
                time.sleep(remain)
    finally:
        with _lock:
            state["stream_running"] = False
            state["stream_frame"]   = None
            state["stream_stats"]   = None
        _add_log("스트리밍 종료")


# ─────────────────────────────────────────────
# 자동 촬영 스레드
# ─────────────────────────────────────────────
def _auto_thread(interval: float, total: "int | None"):
    state["auto_count"] = 0
    state["_auto_stop"].clear()

    while state["auto_running"]:
        count = state["auto_count"] + 1
        _do_capture(f"자동_{count:04d}")
        with _lock:
            state["auto_count"] = count
        if total is not None and count >= total:
            _add_log(f"자동 촬영 완료 — 총 {count}장")
            break
        if state["_auto_stop"].wait(interval):
            break

    with _lock:
        state["auto_running"] = False
    _add_log("자동 촬영 종료")


# ─────────────────────────────────────────────
# 라우트
# ─────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", media_type="text/html; charset=utf-8")


# ── 카메라 ──
@app.post("/api/camera/connect")
async def api_cam_connect(request: Request):
    body = await request.json()
    ip             = body.get("ip", "192.168.0.100").strip()
    port           = int(body.get("port", 80))
    snapshot_path  = body.get("snapshot_path") or None   # 예: "/snapshot.jpg"

    with _lock:
        if state["cam_connected"]:
            return {"ok": True, "msg": "이미 연결됨"}

    cam = FlirA70Camera()
    if not cam.connect(ip, port=port, snapshot_path=snapshot_path):
        return JSONResponse(
            {"ok": False, "msg": f"{ip} 에 연결할 수 없습니다. "
             "IP와 카메라 HTTP 설정을 확인하세요."}, 400)

    with _lock:
        state["camera"]        = cam
        state["cam_connected"] = True
        state["cam_ip"]        = ip

    _add_log(f"카메라 연결 — {cam.snapshot_url}")
    return {"ok": True, "ip": ip, "snapshot_url": cam.snapshot_url}


@app.post("/api/camera/disconnect")
async def api_cam_disconnect():
    with _lock:
        if not state["cam_connected"]:
            return {"ok": True}
        state["stream_running"] = False
        state["auto_running"]   = False
        state["_auto_stop"].set()
        cam = state["camera"]
        state["camera"]        = None
        state["cam_connected"] = False
        state["cam_ip"]        = None
    try:
        cam.disconnect()
    except Exception:
        pass
    _add_log("카메라 연결 해제")
    return {"ok": True}


# ── Arduino ──
@app.post("/api/arduino/connect")
async def api_ard_connect(request: Request):
    body     = await request.json()
    port     = body.get("port") or None
    baudrate = int(body.get("baudrate", 9600))

    with _lock:
        if state["ard_connected"]:
            return {"ok": True, "msg": "이미 연결됨"}

    ard = ArduinoReader()
    if not ard.connect(port=port, baudrate=baudrate):
        try:
            import serial.tools.list_ports
            ports = [p.device for p in serial.tools.list_ports.comports()]
        except Exception:
            ports = []
        return JSONResponse({"ok": False, "msg": "Arduino 연결 실패",
                             "available_ports": ports}, 400)

    with _lock:
        state["arduino"]       = ard
        state["ard_connected"] = True
        state["ard_port"]      = ard.port
    _add_log(f"Arduino 연결 — {ard.port}")
    return {"ok": True, "port": ard.port}


@app.post("/api/arduino/disconnect")
async def api_ard_disconnect():
    with _lock:
        ard = state["arduino"]
        state["arduino"]       = None
        state["ard_connected"] = False
        state["ard_port"]      = None
    if ard:
        ard.disconnect()
    _add_log("Arduino 연결 해제")
    return {"ok": True}


@app.get("/api/arduino/latest")
async def api_ard_latest():
    with _lock:
        ard: ArduinoReader = state["arduino"]
    data = ard.get_latest() if (ard and ard.connected) else None
    return {"ok": True, "data": data}


@app.get("/api/arduino/history")
async def api_ard_history():
    with _lock:
        ard: ArduinoReader = state["arduino"]
    history = ard.get_history(100) if (ard and ard.connected) else []
    return {"ok": True, "history": history}


# ── 촬영 ──
@app.post("/api/capture")
async def api_capture():
    result = _do_capture()
    if result is None:
        return JSONResponse({"ok": False, "msg": "촬영 실패"}, 500)
    return {"ok": True, **result}


# ── 자동 촬영 ──
@app.post("/api/auto/start")
async def api_auto_start(request: Request):
    body      = await request.json()
    interval  = float(body.get("interval", 60))
    total_raw = body.get("total")
    total     = int(total_raw) if total_raw else None

    with _lock:
        if not state["cam_connected"]:
            return JSONResponse({"ok": False, "msg": "카메라 미연결"}, 400)
        if state["auto_running"]:
            return JSONResponse({"ok": False, "msg": "자동 촬영 중"}, 400)
        if state["stream_running"]:
            return JSONResponse({"ok": False, "msg": "스트리밍 중"}, 400)
        state["auto_running"]  = True
        state["auto_total"]    = total
        state["auto_interval"] = interval

    threading.Thread(target=_auto_thread, args=(interval, total), daemon=True).start()
    _add_log(f"자동 촬영 시작 — 간격={interval}s, 횟수={'무제한' if total is None else total}")
    return {"ok": True, "interval": interval, "total": total}


@app.post("/api/auto/stop")
async def api_auto_stop():
    with _lock:
        state["auto_running"] = False
        state["_auto_stop"].set()
    _add_log("자동 촬영 중지")
    return {"ok": True}


# ── 스트리밍 ──
@app.post("/api/stream/start")
async def api_stream_start(request: Request):
    body = await request.json()
    fps  = max(0.1, min(float(body.get("fps", 1.0)), 10.0))

    with _lock:
        if not state["cam_connected"]:
            return JSONResponse({"ok": False, "msg": "카메라 미연결"}, 400)
        if state["stream_running"]:
            return JSONResponse({"ok": False, "msg": "이미 스트리밍 중"}, 400)
        if state["auto_running"]:
            return JSONResponse({"ok": False, "msg": "자동 촬영 중"}, 400)
        state["stream_running"] = True
        state["stream_fps"]     = fps

    threading.Thread(target=_stream_thread, args=(fps,), daemon=True).start()
    return {"ok": True, "fps": fps}


@app.post("/api/stream/stop")
async def api_stream_stop():
    with _lock:
        state["stream_running"] = False
    _add_log("스트리밍 중지")
    return {"ok": True}


@app.get("/api/stream/feed")
async def api_stream_feed():
    async def generate():
        while True:
            if not state["stream_running"]:
                break
            with state["_stream_lock"]:
                frame = state["stream_frame"]
            if frame:
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
            await asyncio.sleep(0.1)
    return StreamingResponse(generate(),
                             media_type="multipart/x-mixed-replace; boundary=frame")


# ── 상태 / 이미지 목록 ──
@app.get("/api/status")
async def api_status():
    with _lock:
        last = state["last_capture"]
        with state["_stream_lock"]:
            stream_stats = state["stream_stats"]
        ard: ArduinoReader = state["arduino"]
        ard_data = ard.get_latest() if (ard and ard.connected) else None
        return {
            "cam_connected":  state["cam_connected"],
            "cam_ip":         state["cam_ip"],
            "ard_connected":  state["ard_connected"],
            "ard_port":       state["ard_port"],
            "auto_running":   state["auto_running"],
            "auto_count":     state["auto_count"],
            "auto_total":     state["auto_total"],
            "auto_interval":  state["auto_interval"],
            "stream_running": state["stream_running"],
            "stream_fps":     state["stream_fps"],
            "stream_stats":   stream_stats,
            "arduino":        ard_data,
            "last_timestamp": last["timestamp"] if last else None,
            "last_temp_mean": last["temp_mean"]  if last else None,
            "logs":           list(state["logs"]),
        }


@app.get("/api/images")
async def api_images():
    files = sorted(CAPTURED_DIR.glob("*_colormap.png"), reverse=True)[:20]
    items = [{"name": f.name,
              "url":  f"/captured/{f.name}",
              "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")}
             for f in files]
    return {"items": items}


@app.get("/api/ports")
async def api_ports():
    try:
        import serial.tools.list_ports
        ports = [{"device": p.device, "description": p.description}
                 for p in serial.tools.list_ports.comports()]
    except Exception:
        ports = []
    return {"ports": ports, "auto_detected": find_arduino_port()}


# ─────────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("  FLIR A70 + Arduino 열화상 시스템")
    print("  접속: http://0.0.0.0:8000")
    print("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
