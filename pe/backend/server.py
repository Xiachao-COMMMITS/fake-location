"""server.py
============================================================
iPhone 定位模拟控制器 - 后端服务。

提供 REST API 供前端调用，并托管前端静态文件。默认监听
http://127.0.0.1:8765 ，启动后自动在浏览器打开。

REST API
--------
GET  /api/health          健康检查 / 环境信息
GET  /api/devices         枚举 USB 连接的 iPhone
POST /api/connect         {udid}            连接设备
POST /api/disconnect      {udid}            断开设备
POST /api/developer-mode/trigger  {udid}    调出 iPhone 开发者模式启用流程
POST /api/developer-mode/reveal   {udid}    令「开发者模式」选项出现在设置菜单
GET  /api/developer-mode/status?udid=...    查询开发者模式状态
POST /api/simulate        {waypoints, speed_kmh, loop_count, jitter}  开始模拟
                         loop_count: 0=单次执行；N>=1=循环 N 次
                         jitter: true=叠加 ±5m 自然跑步随机误差
POST /api/pause                             暂停
POST /api/resume                           继续
POST /api/stop                             停止模拟
POST /api/restore                          恢复真实定位
GET  /api/status                           当前状态
POST /api/exit                             恢复定位并退出程序

启动：python server.py [--dry-run] [--host 127.0.0.1] [--port 8765]
============================================================
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# 确保能 import 同目录模块
sys.path.insert(0, str(Path(__file__).resolve().parent))

from device_manager import DeviceError, DeviceManager  # noqa: E402
from route_simulator import RouteSimulator, haversine_km  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("server")

# --------------------------------------------------------------------------- #
# 全局状态
# --------------------------------------------------------------------------- #
class AppState:
    def __init__(self, dry_run: bool):
        self.dm = DeviceManager(dry_run=dry_run)
        self.dry_run = dry_run
        self.current_udid: Optional[str] = None
        self.sim: Optional[RouteSimulator] = None
        self.last_status: dict = {}

    @property
    def session(self):
        if not self.current_udid:
            return None
        return self.dm.get_session(self.current_udid)


STATE = AppState(dry_run="--dry-run" in sys.argv)

app = FastAPI(title="iPhone 定位模拟控制器", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


# --------------------------------------------------------------------------- #
# 请求模型
# --------------------------------------------------------------------------- #
class ConnectReq(BaseModel):
    udid: str


class Waypoint(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)


class SimulateReq(BaseModel):
    udid: Optional[str] = None
    waypoints: list[Waypoint]
    speed_kmh: float = Field(..., gt=0, le=2000)
    loop_count: int = Field(0, ge=0, le=10000)
    jitter: bool = False


class RouteInfoReq(BaseModel):
    waypoints: list[Waypoint]
    speed_kmh: float = Field(..., gt=0, le=2000)


class DevModeReq(BaseModel):
    udid: str


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _require_session():
    if STATE.session is None:
        raise HTTPException(status_code=400, detail="尚未连接设备，请先连接。")
    return STATE.session


def _require_sim():
    if STATE.sim is None:
        raise HTTPException(status_code=400, detail="当前没有进行中的模拟。")
    return STATE.sim


def _on_status_update(status):
    STATE.last_status = status.to_dict()


def _stop_sim_silent():
    if STATE.sim:
        try:
            STATE.sim.stop()
        except Exception:  # pragma: no cover
            logger.exception("停止模拟失败")
        STATE.sim = None


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    return {
        "ok": True,
        "dry_run": STATE.dry_run,
        "pmd3_available": __import__("device_manager")._PMD3_AVAILABLE,
        "version": "1.0.0",
    }


@app.get("/api/devices")
def devices():
    try:
        return {"devices": [d.to_dict() for d in STATE.dm.list_devices()]}
    except Exception as exc:
        logger.exception("枚举设备失败")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/diagnostics")
def diagnostics():
    """设备检测诊断：逐层检查 usbmuxd / pymobiledevice3 / Windows PnP，给出排错建议。"""
    try:
        return STATE.dm.diagnose()
    except Exception as exc:
        logger.exception("诊断失败")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/connect")
def connect(req: ConnectReq):
    try:
        session = STATE.dm.connect(req.udid)
        STATE.current_udid = req.udid
        logger.info("已连接设备: %s", session.info.name)
        return {"device": session.info.to_dict()}
    except DeviceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("连接失败")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/disconnect")
def disconnect(req: ConnectReq):
    _stop_sim_silent()
    STATE.dm.disconnect(req.udid)
    if STATE.current_udid == req.udid:
        STATE.current_udid = None
    return {"ok": True}


# ---- 开发者模式触发 (iOS 16+，经普通 lockdown，无需隧道) ---- #
@app.post("/api/developer-mode/trigger")
def devmode_trigger(req: DevModeReq):
    """在 iPhone 上调出「启用开发者模式」流程并要求重启。"""
    try:
        return STATE.dm.trigger_developer_mode(req.udid)
    except DeviceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("触发开发者模式失败")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/developer-mode/reveal")
def devmode_reveal(req: DevModeReq):
    """仅令「开发者模式」选项出现在 iPhone 设置菜单（不自动启用）。"""
    try:
        return STATE.dm.reveal_developer_mode(req.udid)
    except DeviceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("显示开发者模式选项失败")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/developer-mode/status")
def devmode_status(udid: str):
    """查询开发者模式当前状态。"""
    try:
        return STATE.dm.developer_mode_status(udid)
    except DeviceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("查询开发者模式状态失败")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/route-info")
def route_info(req: RouteInfoReq):
    """根据航点与速度计算总距离、预计耗时。"""
    pts = [(w.lat, w.lon) for w in req.waypoints]
    if len(pts) < 2:
        raise HTTPException(status_code=400, detail="至少需要 2 个航点")
    total = sum(haversine_km(*pts[i], *pts[i + 1]) for i in range(len(pts) - 1))
    eta_sec = (total / req.speed_kmh) * 3600 if req.speed_kmh > 0 else 0
    return {"total_km": round(total, 4), "eta_seconds": round(eta_sec, 1)}


@app.post("/api/simulate")
def simulate(req: SimulateReq):
    session = STATE.session
    if session is None:
        # 允许通过 udid 自动连接
        if req.udid:
            session = STATE.dm.connect(req.udid)
            STATE.current_udid = req.udid
        else:
            raise HTTPException(status_code=400, detail="尚未连接设备，请先连接。")

    # 已有模拟在跑则先停掉
    _stop_sim_silent()

    pts = [(w.lat, w.lon) for w in req.waypoints]
    try:
        sim = RouteSimulator(
            session=session,
            waypoints=pts,
            speed_kmh=req.speed_kmh,
            step_hz=1.0,
            loop_count=req.loop_count,
            jitter=req.jitter,
            on_update=_on_status_update,
        )
        sim.start()
        STATE.sim = sim
        logger.info("开始模拟: %d 航点, %.1f km/h, 总距离 %.3f km, 循环 %d 次, 随机误差=%s",
                    len(pts), req.speed_kmh, sim.total_km, sim.total_loops,
                    "开" if req.jitter else "关")
        return {"ok": True, "total_km": round(sim.total_km, 4),
                "eta_seconds": round((sim.total_km / req.speed_kmh) * 3600, 1)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except DeviceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("启动模拟失败")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/pause")
def pause():
    sim = _require_sim()
    sim.pause()
    return {"state": sim.status.state}


@app.post("/api/resume")
def resume():
    sim = _require_sim()
    sim.resume()
    return {"state": sim.status.state}


@app.post("/api/stop")
def stop():
    sim = _require_sim()
    sim.stop()
    STATE.sim = None
    return {"state": sim.status.state}


@app.post("/api/restore")
def restore():
    """恢复真实定位：停止模拟并清除定位覆盖。"""
    _stop_sim_silent()
    session = STATE.session
    detail = ""
    if session:
        try:
            session.clear_location()
            logger.info("已清除定位覆盖，手机恢复真实 GPS")
        except DeviceError as exc:
            detail = str(exc)
        except Exception as exc:
            logger.exception("清除定位失败")
            detail = str(exc)
    return {"ok": not detail, "detail": detail}


@app.get("/api/status")
def status():
    connected = STATE.session.info.to_dict() if STATE.session else None
    sim_status = STATE.sim.status.to_dict() if STATE.sim else (STATE.last_status or {"state": "idle"})
    return {"connected_device": connected, "sim": sim_status}


@app.post("/api/exit")
def exit_app():
    """恢复定位并安全退出程序。"""
    logger.info("收到退出请求，正在清理...")
    _stop_sim_silent()
    if STATE.session:
        try:
            STATE.session.clear_location()
        except Exception:  # pragma: no cover
            logger.exception("退出时清除定位失败")
    STATE.dm.disconnect_all()

    def _shutdown():
        time.sleep(0.3)
        logger.info("再见！")
        os._exit(0)

    threading.Thread(target=_shutdown, daemon=True).start()
    return JSONResponse({"ok": True, "message": "正在退出..."})


# --------------------------------------------------------------------------- #
# 静态前端托管
# --------------------------------------------------------------------------- #
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
else:
    @app.get("/")
    def index_fallback():
        return JSONResponse(
            {"error": "前端目录不存在", "expected": str(FRONTEND_DIR)},
            status_code=500,
        )


# --------------------------------------------------------------------------- #
# 启动入口
# --------------------------------------------------------------------------- #
def _parse_args():
    host = "127.0.0.1"
    port = 8765
    no_browser = "--no-browser" in sys.argv
    for i, a in enumerate(sys.argv):
        if a == "--host" and i + 1 < len(sys.argv):
            host = sys.argv[i + 1]
        if a == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])
    return host, port, no_browser


def main():
    import uvicorn

    host, port, no_browser = _parse_args()
    url = f"http://{host}:{port}"
    logger.info("服务地址: %s (dry-run=%s)", url, STATE.dry_run)
    if not no_browser:
        threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(url)), daemon=True).start()
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
