"""device_manager.py
============================================================
iPhone 设备连接、开发者模式触发与定位控制封装 (pymobiledevice3 9.x)。

关键事实 (实测 pymobiledevice3 9.33.1)
--------------------------------------
- `com.apple.dt.simulatelocation` (DtSimulateLocation) 仅适用于 iOS < 17，
  经普通 USB lockdown 即可，无需 DDI / 隧道。
- iOS 17+ (含 iPhone 16 / iOS 18) 的定位模拟必须经 DVT
  (`com.apple.instruments.server.services.LocationSimulation`)，且需要
  RemoteXPC 隧道。本项目使用 `--userspace` 纯 Python 隧道，**无需管理员**。
- 上述 service 的 set/clear 均为 async，故本模块在后台线程跑一个
  asyncio 事件循环，同步接口内部把协程投递到该循环执行。
- 开发者模式触发：`pymobiledevice3 amfi enable-developer-mode` /
  `reveal-developer-mode` / `developer-mode-status`，经普通 lockdown 即可
  调用，会在 iPhone 上调出开发者模式启用界面 (iOS 16+ 菜单默认隐藏)。
- iOS 16+ 使用任何开发者服务前必须先开启开发者模式。
============================================================
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("device_manager")


class DeviceError(RuntimeError):
    """设备相关错误，message 含排错指引。"""


# --------------------------------------------------------------------------- #
# pymobiledevice3 可选导入
# --------------------------------------------------------------------------- #
_PMD3_AVAILABLE = False
_list_devices = None
_create_lockdown = None
_DtSimulateLocation = None
_userspace_tunnel = None
_DvtProvider = None
_LocationSimulation = None

try:
    from pymobiledevice3.usbmux import list_devices as _list_devices  # type: ignore
    from pymobiledevice3.lockdown import create_using_usbmux as _create_lockdown  # type: ignore
    from pymobiledevice3.services.simulate_location import DtSimulateLocation as _DtSimulateLocation  # type: ignore
    from pymobiledevice3.remote import userspace_tunnel as _userspace_tunnel  # type: ignore
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider as _DvtProvider  # type: ignore
    from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation as _LocationSimulation  # type: ignore

    _PMD3_AVAILABLE = True
except Exception as exc:  # pragma: no cover
    logger.warning("pymobiledevice3 不可用 (%s)；将以 dry-run 模式运行", exc)


# --------------------------------------------------------------------------- #
# 后台 asyncio 事件循环（供 sync 接口投递协程）
# --------------------------------------------------------------------------- #
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_lock = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _loop_lock:
        if _loop is None or not _loop.is_running():
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True, name="pmd3-async").start()
        return _loop


def _run_coro(coro, timeout: float = 60.0):
    """把协程投递到后台循环同步等待结果。"""
    fut = asyncio.run_coroutine_threadsafe(coro, _get_loop())
    return fut.result(timeout=timeout)


# --------------------------------------------------------------------------- #
# pymobiledevice3 CLI 可执行文件
# --------------------------------------------------------------------------- #
def _pmd3_exe() -> Optional[str]:
    here = os.path.dirname(sys.executable)
    for name in ("pymobiledevice3.exe", "pymobiledevice3"):
        p = os.path.join(here, name)
        if os.path.exists(p):
            return p
    return shutil.which("pymobiledevice3")


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class DeviceInfo:
    udid: str
    name: str
    product: str = ""
    ios_version: str = ""
    ios_major: int = 0

    def to_dict(self) -> dict:
        return {
            "udid": self.udid,
            "name": self.name,
            "product": self.product,
            "ios_version": self.ios_version,
            "ios_major": self.ios_major,
        }


# --------------------------------------------------------------------------- #
# 设备会话
# --------------------------------------------------------------------------- #
class DeviceSession:
    """一个已连接设备的会话，按 iOS 版本选择定位路径，常驻 service 实例。"""

    def __init__(self, info: DeviceInfo, dry_run: bool = False):
        self.info = info
        self.dry_run = dry_run
        self._lockdown = None
        self._rsd = None
        self._dvt_cm = None
        self._dvt = None
        self._loc_cm = None
        self._loc = None  # DtSimulateLocation (<17) 或 LocationSimulation (>=17)
        self._lock = threading.Lock()
        self._opened = False

    # ---- 生命周期 ---- #
    def open(self) -> None:
        if self.dry_run:
            self._opened = True
            return
        if not _PMD3_AVAILABLE:
            raise DeviceError("pymobiledevice3 未安装，无法连接真机。")
        try:
            if self.info.ios_major >= 17:
                self._open_ios17()
            else:
                self._open_legacy()
            self._opened = True
        except DeviceError:
            raise
        except Exception as exc:
            raise _translate_error(exc, self.info.udid) from exc

    def _open_legacy(self) -> None:
        """iOS < 17：DtSimulateLocation 经普通 lockdown（create_using_usbmux 为 async）。"""
        try:
            self._lockdown = _run_coro(_create_lockdown(udid=self.info.udid), timeout=60)
        except Exception as exc:
            raise _translate_error(exc, self.info.udid) from exc
        # 构造同步；set/clear 内部异步开启服务连接，若开发者模式未开会抛错
        self._loc = _DtSimulateLocation(self._lockdown)

    def _open_ios17(self) -> None:
        """iOS 17+：userspace 隧道 → DvtProvider → LocationSimulation 常驻。"""
        # 1. 建立 userspace RSD 隧道 (免管理员)
        try:
            self._rsd = _run_coro(
                _userspace_tunnel.establish_userspace_rsd(serial=self.info.udid, autopair=True),
                timeout=120,
            )
        except Exception as exc:
            raise DeviceError(
                f"建立 iOS 17+ 隧道失败。请确认已开启开发者模式并重启手机。原因: {exc}"
            ) from exc

        # 2. 进入 DvtProvider + LocationSimulation 上下文并常驻
        async def _enter():
            self._dvt_cm = _DvtProvider(self._rsd)
            self._dvt = await self._dvt_cm.__aenter__()
            self._loc_cm = _LocationSimulation(self._dvt)
            self._loc = await self._loc_cm.__aenter__()

        try:
            _run_coro(_enter(), timeout=180)  # DDI 挂载可能较慢
        except Exception:
            # 清理已建立的部分
            self._safe_close()
            raise

    # ---- 定位控制 ---- #
    def set_location(self, lat: float, lon: float) -> None:
        if self.dry_run:
            logger.info("[dry-run] set_location(%.6f, %.6f)", lat, lon)
            return
        if not self._opened or self._loc is None:
            raise DeviceError("设备会话未就绪。")
        with self._lock:
            try:
                _run_coro(self._loc.set(lat, lon), timeout=20)
            except Exception as exc:
                raise DeviceError(f"设置定位失败: {exc}") from exc

    def clear_location(self) -> None:
        if self.dry_run:
            logger.info("[dry-run] clear_location()")
            return
        if not self._opened or self._loc is None:
            return
        with self._lock:
            try:
                _run_coro(self._loc.clear(), timeout=20)
            except Exception as exc:
                logger.warning("清除定位失败: %s", exc)
                raise DeviceError(f"清除定位失败: {exc}") from exc

    # ---- 关闭 ---- #
    def close(self) -> None:
        self._safe_close()

    def _safe_close(self) -> None:
        with self._lock:
            # 逆序退出上下文
            if self._loc_cm is not None:
                try:
                    _run_coro(self._loc_cm.__aexit__(None, None, None), timeout=15)
                except Exception:  # pragma: no cover
                    logger.debug("退出 LocationSimulation 上下文失败", exc_info=True)
                self._loc_cm = None
                self._loc = None
            if self._dvt_cm is not None:
                try:
                    _run_coro(self._dvt_cm.__aexit__(None, None, None), timeout=15)
                except Exception:  # pragma: no cover
                    logger.debug("退出 DvtProvider 上下文失败", exc_info=True)
                self._dvt_cm = None
                self._dvt = None
            self._rsd = None
            if self._lockdown is not None:
                try:
                    _run_coro(self._lockdown.close(), timeout=15)
                except Exception:  # pragma: no cover
                    logger.debug("关闭 lockdown 失败", exc_info=True)
                self._lockdown = None
            self._opened = False


# --------------------------------------------------------------------------- #
# 设备管理器
# --------------------------------------------------------------------------- #
class DeviceManager:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self._sessions: dict[str, DeviceSession] = {}

    # ---- 枚举 ---- #
    def list_devices(self) -> list[DeviceInfo]:
        if self.dry_run:
            return [DeviceInfo(udid="DRY-RUN-DEVICE", name="模拟设备 (dry-run)", product="iPhone Dry", ios_version="99.0", ios_major=99)]
        if not _PMD3_AVAILABLE:
            return []
        # 9.33.1 的 list_devices() / create_using_usbmux() 均为 async；CLI `usbmux list`
        # 同步、默认输出完整 JSON（含 DeviceName/ProductType/ProductVersion/UniqueDeviceID），
        # 故以 CLI 为主，避免在枚举阶段引入事件循环。
        devices = self._list_devices_cli()
        if devices:
            return devices
        # CLI 为空时尝试程序化 async 路径兜底
        return self._list_devices_async()

    def _list_devices_async(self) -> list[DeviceInfo]:
        if not _PMD3_AVAILABLE:
            return []
        try:
            devs = _run_coro(_list_devices(), timeout=20)
        except Exception as exc:
            logger.warning("程序化枚举失败: %s", exc)
            return []
        result: list[DeviceInfo] = []
        for usb_dev in devs:
            udid = (getattr(usb_dev, "serial", None) or getattr(usb_dev, "udid", None)
                    or getattr(usb_dev, "UniqueDeviceID", None))
            if not udid:
                continue
            result.append(self._find_device_info(udid) or DeviceInfo(udid=udid, name=udid))
        return result

    def _find_device_info(self, udid: str) -> Optional[DeviceInfo]:
        """从 CLI 枚举结果中查找指定 udid 的设备信息。"""
        for d in self._list_devices_cli():
            if d.udid == udid:
                return d
        return None

    def _list_devices_cli(self) -> list[DeviceInfo]:
        """经 pymobiledevice3 usbmux list 枚举（9.33.1 默认输出 JSON，无 --json 选项）。"""
        exe = _pmd3_exe()
        if not exe:
            return []
        try:
            r = subprocess.run([exe, "usbmux", "list"],
                               capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return []
            out = (r.stdout or "").strip()
            if not out:
                return []
            import json
            import re
            # 输出可能混有日志行，提取首个 JSON 数组
            m = re.search(r'\[.*\]', out, re.S)
            data = json.loads(m.group(0)) if m else json.loads(out)
            result = []
            for d in data:
                udid = (d.get("UniqueDeviceID") or d.get("Identifier")
                        or d.get("UDID") or d.get("udid") or d.get("serial", ""))
                if not udid:
                    continue
                ver = d.get("ProductVersion", "")
                result.append(DeviceInfo(udid=udid, name=d.get("DeviceName", udid),
                                         product=d.get("ProductType", ""), ios_version=ver,
                                         ios_major=_parse_major(ver)))
            return result
        except Exception as exc:
            logger.warning("CLI 枚举失败: %s", exc)
            return []

    # ---- 连接 ---- #
    def connect(self, udid: str) -> DeviceSession:
        if udid in self._sessions and self._sessions[udid]._opened:
            return self._sessions[udid]

        if self.dry_run:
            session = DeviceSession(
                info=DeviceInfo(udid=udid, name=f"模拟设备 {udid[:8]}", product="iPhone Dry",
                                ios_version="99.0", ios_major=99),
                dry_run=True,
            )
            session.open()
            self._sessions[udid] = session
            return session

        if not _PMD3_AVAILABLE:
            raise DeviceError("pymobiledevice3 未安装。请运行: pip install pymobiledevice3")

        # 1. 从 CLI 枚举取设备信息（create_using_usbmux 在 9.33.1 是 async，CLI 同步且已含全部字段）
        info = self._find_device_info(udid)
        if info is None:
            raise DeviceError(
                f"未找到设备 {udid[:8]}…。请确认: ① 手机已用 USB 数据线连接；"
                "② 手机已解锁并信任电脑；③ 已安装 iTunes/Apple Devices 提供 usbmuxd。"
            )

        # 2. iOS 16+ 需先确认开发者模式已开（开发者模式未开则定位服务不可用）
        if info.ios_major >= 16:
            status = self._developer_mode_status(udid)
            if status.get("enabled") is False:
                raise DeviceError(
                    "开发者模式未开启（这是 iOS 16+ 使用定位模拟的前提）。\n"
                    "请点界面上的「🔧 开启开发者模式」按钮，按提示在手机上确认并重启，"
                    "重启后回到这里重新点「连接」。"
                )

        # 3. 打开定位会话
        session = DeviceSession(info=info)
        session.open()
        self._sessions[udid] = session
        logger.info("已连接设备: %s (iOS %s, major=%s)", info.name, info.ios_version, info.ios_major)
        return session

    def get_session(self, udid: str) -> Optional[DeviceSession]:
        return self._sessions.get(udid)

    def disconnect(self, udid: str) -> None:
        s = self._sessions.pop(udid, None)
        if s:
            s.close()

    def disconnect_all(self) -> None:
        for s in list(self._sessions.values()):
            try:
                s.close()
            except Exception:  # pragma: no cover
                pass
        self._sessions.clear()

    # ---- 诊断 ---- #
    def diagnose(self) -> dict:
        """返回结构化诊断信息，帮助定位"未发现设备"问题。

        逐层检查：pymobiledevice3 可用性 → usbmuxd TCP 端口 → CLI 枚举 →
        异步枚举 → Windows PnP。当某一层失败时给出对应的排错建议。
        """
        import json as _json
        import socket

        report: dict = {
            "pmd3_available": _PMD3_AVAILABLE,
            "usbmuxd_tcp": False,
            "cli_devices": [],
            "async_devices": [],
            "pnp_apple_devices": [],
            "recommendations": [],
        }

        # 1. TCP 连接 usbmuxd (127.0.0.1:27015)
        try:
            with socket.create_connection(("127.0.0.1", 27015), timeout=3):
                report["usbmuxd_tcp"] = True
        except Exception:
            report["usbmuxd_tcp"] = False
            report["recommendations"].append(
                "usbmuxd 服务未在 127.0.0.1:27015 监听。请安装 iTunes 或 Microsoft Store"
                "「Apple Devices」应用，以提供 Apple Mobile Device Service。"
            )

        # 2. CLI 枚举
        try:
            report["cli_devices"] = [d.to_dict() for d in self._list_devices_cli()]
        except Exception as e:
            report["recommendations"].append(f"CLI 枚举异常: {e}")

        # 3. 异步枚举
        try:
            report["async_devices"] = [d.to_dict() for d in self._list_devices_async()]
        except Exception as e:
            report["recommendations"].append(f"异步枚举异常: {e}")

        # 4. Windows PnP 检测 Apple 设备
        if sys.platform == "win32":
            try:
                ps_cmd = (
                    "Get-PnpDevice -ErrorAction SilentlyContinue | "
                    "Where-Object { $_.FriendlyName -like '*Apple*' "
                    "-or $_.FriendlyName -like '*iPhone*' "
                    "-or $_.Manufacturer -like '*Apple*' } | "
                    "Select-Object FriendlyName,Status,Class | ConvertTo-Json"
                )
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    capture_output=True, text=True, timeout=10,
                )
                out = (r.stdout or "").strip()
                if out:
                    pnp = _json.loads(out)
                    report["pnp_apple_devices"] = pnp if isinstance(pnp, list) else [pnp]
            except Exception:
                pass

        # 5. 综合建议
        has_cli = bool(report["cli_devices"])
        has_async = bool(report["async_devices"])
        has_pnp = bool(report["pnp_apple_devices"])

        if not _PMD3_AVAILABLE:
            report["recommendations"].insert(0,
                "pymobiledevice3 未安装。请在后端虚拟环境中运行: "
                "pip install pymobiledevice3")
        elif not has_cli and not has_async:
            if not report["usbmuxd_tcp"]:
                pass  # 已在上方添加建议
            elif has_pnp:
                report["recommendations"].insert(0,
                    "Windows 已识别到 Apple 设备，但 pymobiledevice3 枚举为空。请依次尝试: "
                    "① 解锁 iPhone 屏幕; ② 拔插 USB 数据线; "
                    "③ 在手机弹窗点「信任」并输入密码; "
                    "④ 若仍不行，打开「设备管理器」→ 找到「Apple Mobile Device USB Device」"
                    "→ 右键卸载设备 → 拔插数据线让系统重装驱动。")
            else:
                report["recommendations"].insert(0,
                    "未检测到任何 Apple 设备。请确认: ① iPhone 已用 USB 数据线连接; "
                    "② 手机已解锁; ③ 已安装 iTunes 或「Apple Devices」应用。")
        else:
            report["recommendations"].insert(0, "✓ 设备检测正常，无需额外操作。")

        return report

    # ---- 开发者模式 (amfi CLI, 经普通 lockdown) ---- #
    def trigger_developer_mode(self, udid: str) -> dict:
        """启用开发者模式：尝试 amfi enable；设了锁屏密码时回退 reveal + 手动指引。"""
        res = self._amfi(udid, "enable-developer-mode", timeout=90, success_msg="")
        out_low = (res.get("output") or "").lower()
        # 设了锁屏密码时 enable 不可用（amfi 报 "Cannot enable developer-mode when passcode is set"）
        if not res.get("ok") and ("passcode" in out_low or "cannot enable" in out_low):
            try:
                self._amfi(udid, "reveal-developer-mode", timeout=60)  # 令选项出现在设置菜单
            except Exception:  # pragma: no cover
                pass
            return {
                "ok": True,
                "output": res.get("output", ""),
                "message": (
                    "你的 iPhone 设了锁屏密码，无法自动启用开发者模式；"
                    "已令「开发者模式」选项出现在设置菜单中。\n"
                    "请手动操作：iPhone「设置 → 隐私与安全性 → 开发者模式」→ 打开 → 输入密码 → 确认重启。"
                    "重启后回到这里点「连接」。"
                ),
                "revealed": True,
            }
        if res.get("ok"):
            res["message"] = ("已向 iPhone 发送「启用开发者模式」请求。请在手机弹窗中确认并重启；"
                              "重启后开发者模式即开启，回到这里点「连接」。")
        else:
            res["message"] = ("启用开发者模式失败：" + (res.get("output") or "未知错误") +
                              "\n可改用「👁 显示菜单」先让选项出现，再到手机设置里手动开启。")
        return res

    def reveal_developer_mode(self, udid: str) -> dict:
        """仅在设置界面显示「开发者模式」选项（不自动启用）。"""
        return self._amfi(udid, "reveal-developer-mode", timeout=60,
                          success_msg="已请求在 iPhone「设置 → 隐私与安全性」中显示「开发者模式」选项。"
                                      "请前往该菜单手动打开并重启。")

    def developer_mode_status(self, udid: str) -> dict:
        """查询开发者模式开关状态（供前端展示）。"""
        if self.dry_run:
            return {"ok": True, "enabled": True, "output": "[dry-run] 开发者模式已模拟为开启",
                    "message": "dry-run 模式下开发者模式视为已开启。"}
        return self._amfi(udid, "developer-mode-status", timeout=30, parse_status=True)

    def _developer_mode_status(self, udid: str) -> dict:
        return self.developer_mode_status(udid)

    def _amfi(self, udid: str, sub: str, timeout: float = 60,
              success_msg: str = "", parse_status: bool = False) -> dict:
        if self.dry_run:
            res = {"ok": True, "output": f"[dry-run] amfi {sub} 模拟成功", "message": success_msg}
            if parse_status:
                res["enabled"] = True
            return res
        exe = _pmd3_exe()
        if not exe:
            raise DeviceError("pymobiledevice3 CLI 未找到，请确认依赖已安装。")
        cmd = [exe, "amfi", sub, "--udid", udid]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise DeviceError(f"amfi {sub} 执行超时。")
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        # pymobiledevice3 日志走 stderr；USB/usbmuxd 错误也在 stderr
        combined = (out + "\n" + err).strip()
        # 去除 ANSI 颜色码便于匹配
        clean = re.sub(r'\x1b\[[0-9;]*m', '', combined)
        low = clean.lower()

        # usbmuxd 不可用
        if "usbmux" in low and "running" in low:
            raise DeviceError("无法连接 usbmuxd。请确认已安装 iTunes 或 Microsoft Store 的「Apple Devices」，"
                              "并已用 USB 连接手机且信任电脑。")

        # amfi 即便 exit 0 也可能在日志里报 ERROR（如设了密码时 enable-developer-mode 失败）
        has_error = ("cannot enable" in low or "passcode is set" in low
                     or " error " in low or " failed" in low)

        result: dict = {"ok": r.returncode == 0 and not has_error, "output": combined, "message": success_msg}
        if parse_status:
            # 解析开发者模式状态
            if "true" in low or "enabled" in low:
                result["enabled"] = True
            elif "false" in low or "disabled" in low or "not" in low:
                result["enabled"] = False
            else:
                # 解析失败：保守视为未开，交由后续服务连接兜底
                result["enabled"] = None
        return result


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _parse_major(version: str) -> int:
    try:
        return int(str(version).split(".")[0])
    except Exception:
        return 0


def _translate_error(exc: Exception, udid: str) -> DeviceError:
    msg = str(exc).lower()
    if any(k in msg for k in ("pair", "trust", "hostid", "setppr", "invalidhostid")):
        return DeviceError(f"设备 {udid[:8]} 尚未信任此电脑。请在手机弹窗点「信任」并输入密码；错过则拔插数据线重试。")
    if any(k in msg for k in ("developer mode", "developermode", "amfi", "developerdisabled")):
        return DeviceError("开发者模式未开启。请点界面上的「🔧 开启开发者模式」按钮，按提示重启后再连接。")
    if any(k in msg for k in ("password", "passcode", "locked", "device locked")):
        return DeviceError("设备未解锁。请解锁手机后再连接。")
    if any(k in msg for k in ("usbmux", "connection", "refused", "not connected", "no device")):
        return DeviceError("无法与设备通信。请确认: ① 已装 iTunes/Apple Devices; ② USB 数据线连接; ③ 手机解锁并信任电脑。")
    return DeviceError(f"连接设备失败: {exc}")
