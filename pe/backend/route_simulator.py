"""route_simulator.py
============================================================
路径模拟引擎：按用户设定的航点序列与速度，在后台线程中持续向
iPhone 推送插值后的坐标，实现平滑的模拟运动。

核心算法
--------
1. 用 haversine 公式计算相邻航点间的大圆距离，得到累计距离表。
2. 以固定频率 (默认 1 Hz) 推进：每步距离 = speed (km/h) / 3.6 * dt (s)。
3. 按累计距离在对应航段内线性插值出当前 lat/lon。
4. 通过 threading.Event 实现暂停 / 继续；通过标志位实现停止。

循环控制
--------
loop_count=0 → 单次执行（跑一遍即停）；loop_count=N (>=1) → 精确重复
执行当前轨迹 N 次。每圈结束后 progress 归零、回到起点继续，current_loop
递增并在 SimStatus 中暴露。

随机误差（自然跑步模拟）
------------------------
jitter=True 时，每步坐标叠加均值回归的平滑随机游走偏移：高斯噪声模拟
步长波动与方向随机性，衰减项使偏移紧贴路径，末尾硬性钳制 |offset|<=5m，
避免白噪声跳变，呈现自然跑步轨迹。计算量极小，不影响 1Hz 流畅度。

线程安全
--------
模拟循环只与 DeviceSession.set_location() 交互，而 DeviceSession 内部
自带锁，因此暂停/停止/恢复的控制调用与模拟线程不会相互破坏。
============================================================
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from device_manager import DeviceSession

logger = logging.getLogger("route_simulator")

EARTH_RADIUS_KM = 6371.0088


# --------------------------------------------------------------------------- #
# 地理计算
# --------------------------------------------------------------------------- #
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点间大圆距离 (km)。"""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """从点1到点2的初始方位角 (0-360, 正北为0)。"""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(rlat2)
    y = math.cos(rlat1) * math.sin(rlat2) - math.sin(rlat1) * math.cos(rlat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _interpolate(lat1, lon1, lat2, lon2, frac):
    """在两点间按 frac (0..1) 线性插值。对短距离 (<100km) 足够平滑。"""
    return lat1 + (lat2 - lat1) * frac, lon1 + (lon2 - lon1) * frac


# --------------------------------------------------------------------------- #
# 模拟状态
# --------------------------------------------------------------------------- #
@dataclass
class SimStatus:
    state: str = "idle"          # idle | running | paused | finished | stopped | error
    progress_km: float = 0.0
    total_km: float = 0.0
    current_lat: Optional[float] = None
    current_lon: Optional[float] = None
    speed_kmh: float = 0.0
    current_loop: int = 1        # 当前循环 (1-based)
    total_loops: int = 1         # 总循环次数 (>=1)
    jitter: bool = False         # 是否启用随机误差
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "progress_km": round(self.progress_km, 4),
            "total_km": round(self.total_km, 4),
            "current_lat": self.current_lat,
            "current_lon": self.current_lon,
            "speed_kmh": self.speed_kmh,
            "current_loop": self.current_loop,
            "total_loops": self.total_loops,
            "jitter": self.jitter,
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# 路径模拟器
# --------------------------------------------------------------------------- #
class RouteSimulator:
    def __init__(
        self,
        session: DeviceSession,
        waypoints: list[tuple[float, float]],
        speed_kmh: float,
        step_hz: float = 1.0,
        loop_count: int = 0,
        jitter: bool = False,
        jitter_meters: float = 5.0,
        on_update: Optional[Callable[[SimStatus], None]] = None,
        on_finish: Optional[Callable[[SimStatus], None]] = None,
    ):
        if len(waypoints) < 2:
            raise ValueError("至少需要 2 个航点才能开始模拟")
        if speed_kmh <= 0:
            raise ValueError("速度必须大于 0")

        self.session = session
        self.waypoints = waypoints
        self.speed_kmh = float(speed_kmh)
        self.step_hz = float(step_hz)
        self.dt = 1.0 / self.step_hz
        self.on_update = on_update
        self.on_finish = on_finish

        # 循环控制：0=单次执行（不循环）；N>=1=精确重复 N 次
        self.total_loops = max(1, int(loop_count))

        # 随机误差（自然跑步模拟）：平滑随机游走 + 步长波动，严格 |offset| <= 5m
        self.jitter = bool(jitter)
        self.jitter_meters = min(5.0, max(0.0, float(jitter_meters)))
        self._jx = 0.0  # 当前东向偏移 (m)
        self._jy = 0.0  # 当前北向偏移 (m)
        self._rng = random.Random()

        # 预计算累计距离表
        self._seg_lengths: list[float] = []
        self._cum: list[float] = [0.0]
        for i in range(len(waypoints) - 1):
            d = haversine_km(*waypoints[i], *waypoints[i + 1])
            self._seg_lengths.append(d)
            self._cum.append(self._cum[-1] + d)
        self.total_km = self._cum[-1]

        self.status = SimStatus(
            total_km=self.total_km, speed_kmh=self.speed_kmh,
            total_loops=self.total_loops, jitter=self.jitter,
        )

        self._thread: Optional[threading.Thread] = None
        self._pause_event = threading.Event()
        self._pause_event.set()  # set = 不暂停
        self._stop_flag = threading.Event()
        self._lock = threading.Lock()

    # ---- 控制 ---- #
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("模拟已在运行中")
            self._stop_flag.clear()
            self._pause_event.set()
            self.status.state = "running"
            self.status.progress_km = 0.0
            self.status.current_loop = 1
            self.status.error = ""
            self._jx = 0.0
            self._jy = 0.0
            self._thread = threading.Thread(target=self._run, name="route-sim", daemon=True)
            self._thread.start()

    def pause(self) -> None:
        self._pause_event.clear()
        with self._lock:
            if self.status.state == "running":
                self.status.state = "paused"
        self._emit()

    def resume(self) -> None:
        with self._lock:
            if self.status.state == "paused":
                self.status.state = "running"
        self._pause_event.set()

    def stop(self) -> None:
        """停止模拟，保持当前模拟位置 (不恢复真实定位)。"""
        self._stop_flag.set()
        self._pause_event.set()  # 释放可能正在等待的线程
        if self._thread:
            self._thread.join(timeout=3.0)
        with self._lock:
            if self.status.state in ("running", "paused"):
                self.status.state = "stopped"
        self._emit()

    # ---- 状态 ---- #
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- 主循环 ---- #
    def _run(self) -> None:
        try:
            for lap in range(1, self.total_loops + 1):
                if self._stop_flag.is_set():
                    break
                with self._lock:
                    self.status.current_loop = lap

                progress = 0.0
                # 每圈起点立即下发一次
                lat, lon = self._point_at(progress)
                self._push(lat, lon, progress)
                self._emit()

                while progress < self.total_km:
                    # 响应停止
                    if self._stop_flag.is_set():
                        break
                    # 响应暂停 (阻塞等待，不消耗 CPU，不推进)
                    self._pause_event.wait()

                    if self._stop_flag.is_set():
                        break

                    t_start = time.monotonic()
                    step_km = (self.speed_kmh / 3.6) * self.dt / 1000.0  # km/h -> km/s * dt
                    progress = min(progress + step_km, self.total_km)
                    lat, lon = self._point_at(progress)
                    self._push(lat, lon, progress)
                    self._emit()

                    # 精确节流：扣除本步已耗时间，保证整体频率稳定
                    elapsed = time.monotonic() - t_start
                    time.sleep(max(0.0, self.dt - elapsed))

                # 本圈结束；若已停止则退出，否则进入下一圈重复轨迹
                if self._stop_flag.is_set():
                    break

            with self._lock:
                if not self._stop_flag.is_set():
                    self.status.state = "finished"
        except Exception as exc:
            logger.exception("模拟线程异常")
            with self._lock:
                self.status.state = "error"
                self.status.error = str(exc)
        finally:
            self._emit()
            if self.on_finish:
                try:
                    self.on_finish(self.status)
                except Exception:  # pragma: no cover
                    logger.exception("on_finish 回调异常")

    # ---- 工具 ---- #
    def _point_at(self, dist_km: float) -> tuple[float, float]:
        """按累计距离 dist_km 在路径上插值出坐标。"""
        if dist_km <= 0:
            return self.waypoints[0]
        if dist_km >= self.total_km:
            return self.waypoints[-1]
        # 找到所在航段
        import bisect
        idx = bisect.bisect_right(self._cum, dist_km) - 1
        idx = max(0, min(idx, len(self.waypoints) - 2))
        seg_start = self._cum[idx]
        seg_len = self._seg_lengths[idx]
        frac = (dist_km - seg_start) / seg_len if seg_len > 0 else 0.0
        lat1, lon1 = self.waypoints[idx]
        lat2, lon2 = self.waypoints[idx + 1]
        return _interpolate(lat1, lon1, lat2, lon2, frac)

    def _push(self, lat: float, lon: float, progress: float) -> None:
        if self.jitter:
            lat, lon = self._apply_jitter(lat, lon)
        self.session.set_location(lat, lon)
        with self._lock:
            self.status.current_lat = lat
            self.status.current_lon = lon
            self.status.progress_km = progress

    def _apply_jitter(self, lat: float, lon: float) -> tuple[float, float]:
        """为坐标叠加自然跑步随机偏移。

        采用均值回归的平滑随机游走 (Ornstein-Uhlenbeck 近似)：
        - 每步叠加高斯噪声，模拟步长波动与方向随机性；
        - 衰减项使偏移紧贴真实路径，避免持续漂离；
        - 末尾硬性钳制 |offset| <= jitter_meters (<=5m)。
        计算量极小 (几次浮点运算)，不影响 1Hz 推进流畅度。
        """
        sigma = 1.2          # 单步噪声标准差 (m)，控制步长/方向波动幅度
        decay = 0.82         # 均值回归系数，越大越容易漂离路径
        self._jx = decay * self._jx + self._rng.gauss(0.0, sigma)
        self._jy = decay * self._jy + self._rng.gauss(0.0, sigma)
        mag = math.hypot(self._jx, self._jy)
        if mag > self.jitter_meters:
            scale = self.jitter_meters / mag
            self._jx *= scale
            self._jy *= scale
        # 米 → 经纬度增量 (近似平球面)
        dlat = self._jy / 111000.0
        cos_lat = max(0.01, math.cos(math.radians(lat)))
        dlon = self._jx / (111000.0 * cos_lat)
        return lat + dlat, lon + dlon

    def _emit(self) -> None:
        if self.on_update:
            try:
                self.on_update(self.status)
            except Exception:  # pragma: no cover
                logger.exception("on_update 回调异常")
