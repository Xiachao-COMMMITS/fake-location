# iPhone 定位模拟控制器

一个运行于 **Windows** 的网页版应用程序，通过 **USB 数据线**连接 iPhone，按用户在地图上规划的路径与速度，**模拟 iPhone 的 GPS 定位运动**。支持开始 / 暂停 / 继续 / 停止、**循环执行 N 次**、**±5m 自然跑步随机误差**、一键恢复真实定位、安全退出。

> 适用场景：App 位置功能开发与测试、定位服务 QA、按既定路线的运动模拟演示。
> **不收集、不存储任何用户真实位置信息**，全部计算与下发均在本地完成。

---

## 一、技术架构（重要：先读懂这一段）

纯浏览器网页出于安全沙箱限制，**无法直接通过 USB 读写 iPhone 定位**。因此本项目采用业界标准架构：

```
┌──────────────┐    HTTP/REST    ┌──────────────────────┐     USB      ┌─────────┐
│  浏览器前端   │ ◄────────────► │  本地 Python 后端     │ ◄──────────► │ iPhone  │
│ (地图/UI/控制)│   127.0.0.1    │  FastAPI + 模拟引擎   │  usbmuxd/DVT │ (iOS16+)│
└──────────────┘                 └──────────────────────┘              └─────────┘
```

- **前端**：Leaflet 世界地图 + 控制面板，跑在浏览器，兼容 Chrome / Firefox / Edge 最新版。
- **后端**：本地 FastAPI 服务，调用 `pymobiledevice3`（libimobiledevice 的纯 Python 实现）经 Apple 官方 `com.apple.dt.simulatelocation` 开发者服务设置/清除定位。
- **iPhone 侧**：iOS 16+ 需开启**开发者模式**；不越狱即可使用——这是 Apple 官方提供给开发者的能力，与 3uTools、iAnyGo 等商业工具原理一致。

启动后程序在 `http://127.0.0.1:8765` 提供服务，并自动打开默认浏览器。

---

## 二、环境准备

### 1. Python（必装）
- 安装 **Python 3.9+**（本项目在 3.12.4 上测试通过）。
- 下载：<https://www.python.org/downloads/>
- 安装时勾选「Add Python to PATH」，并保留 `py` 启动器。

### 2. Apple USB 通信驱动（真机模式必装）
pymobiledevice3 在 Windows 上依赖 `usbmuxd` 通道，由以下任一方式提供：
- 安装 **iTunes**（含 Apple Mobile Device Support）；或
- 从 Microsoft Store 安装 **「Apple Devices」** 应用（更轻量，推荐 Win11）。

### 3. iPhone 侧准备（真机模式）
1. 用 **USB 数据线**连接 iPhone 到电脑。
2. **解锁手机**，弹窗点击「**信任此电脑**」并输入锁屏密码。
3. iOS 16+：进入「**设置 → 隐私与安全性 → 开发者模式**」→ 打开 → 重启。
   > **若菜单中看不到「开发者模式」选项**（iOS 16+ 默认隐藏）：在本程序网页左侧「① 设备连接」选中设备后，点「**🔧 开启开发者模式**」按钮，程序会经 Apple 官方 `amfi` 通道在手机上**自动调出开发者模式启用窗口**——按提示点「启用」→ 输入密码 → 确认重启即可；重启后该菜单会出现并已开启。若只想让选项先显示出来再手动开，点旁边的「**👁 显示菜单**」。
   > 该按钮调用 `pymobiledevice3 amfi enable-developer-mode / reveal-developer-mode`，经普通 USB lockdown 即可，无需隧道或管理员权限。

---

## 三、安装与运行

### 方式 A：一键启动（推荐）

双击 **`启动.bat`**（它调用 `launch.ps1`）。脚本会自动完成全部准备工作并在打开前端网页时同步启动所有必要的后端程序：

1. 检测 Python；
2. 创建虚拟环境（首次约 10 秒）；
3. 安装依赖（**默认走清华 PyPI 镜像**，国内不超时；首次安装 pymobiledevice3 约 2–5 分钟）；
4. 启动后端服务；
5. 等待后端就绪后**自动打开浏览器**前端网页；
6. 启动器窗口随后端生命周期保持运行，关闭后端即自动退出。

```
启动.bat          # 真机模式（自动检测 pymobiledevice3，装不上则回退 dry-run）
启动.bat dry      # 强制模拟模式（无需真机，可体验全部 UI 流程，用于演示/测试）
```

> 说明：依赖装不上时脚本会自动以 **dry-run 模拟模式**启动，确保网页总能打开、流程总能体验；真机功能可稍后手动补装依赖再重启。
> 旧版 `run.bat` 仍保留可用，逻辑等价。

### 方式 B：手动命令行

```powershell
# 在项目根目录
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r backend\requirements.txt
.\.venv\Scripts\python.exe backend\server.py            # 真机模式
.\.venv\Scripts\python.exe backend\server.py --dry-run  # 模拟模式
```

常用参数：`--host`、`--port`、`--no-browser`、`--dry-run`。

启动后访问：<http://127.0.0.1:8765>

---

## 四、快速验证（dry-run 模式）

无需真机即可验证整套流程：

1. 运行 `run.bat dry`。
2. 浏览器打开后，左侧「设备连接」点「🔄」→ 选「模拟设备 (dry-run)」→「连接」。
3. 地图上点击添加 ≥2 个航点，设置速度。
4. 点「▶ 开始」，观察红色实时点沿路径移动、进度条推进；测试「暂停/继续/停止」。
5. 点「🔄 恢复实际定位」→ 日志显示清除操作（dry-run 下不下发真机）。

---

## 五、项目结构

```
pe/
├── backend/
│   ├── server.py            # FastAPI 服务 + REST 接口 + 静态托管
│   ├── device_manager.py    # pymobiledevice3 封装：设备枚举/连接/定位控制/开发者模式触发
│   ├── route_simulator.py   # 路径模拟引擎：插值运动/暂停/停止/循环N次/±5m随机误差
│   └── requirements.txt     # 后端依赖
├── frontend/
│   ├── index.html           # 页面结构
│   ├── styles.css           # 暗色主题样式
│   └── app.js               # 地图/路径/速度/控制/开发者模式/状态轮询逻辑
├── docs/
│   ├── USER_MANUAL.md       # 用户操作手册
│   └── DEV_TEST_REPORT.md   # 开发与测试报告（含兼容性测试）
├── 启动.bat                  # 一键启动入口（推荐）
├── launch.ps1               # 启动器脚本：自动装依赖+起后端+开网页（启动.bat 调用）
├── run.bat                  # 旧版启动脚本（保留可用）
└── README.md                # 本文件
```

---

## 六、常见问题排查

| 现象 | 原因 / 解决 |
|------|------------|
| 顶部徽章显示「pymobiledevice3 未安装」 | 依赖未装好。运行 `.\.venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r backend\requirements.txt` |
| 「未发现设备」 | ① 未装 iTunes/Apple Devices；② 非 USB 连接；③ 手机未解锁/未信任；④ 换一根数据线（部分线仅充电） |
| 连接报「尚未信任此电脑」 | 手机弹窗点「信任」并输入密码；错过则拔插数据线重试 |
| 连接报「未开启开发者模式」 | 选中设备后点「🔧 开启开发者模式」，按提示在手机弹窗确认并重启；重启后回这里点「连接」 |
| iPhone 设置里找不到「开发者模式」选项 | iOS 16+ 默认隐藏。选中设备后点「👁 显示菜单」令选项出现，或直接点「🔧 开启开发者模式」走自动启用流程 |
| 模拟已开始但 iPhone 定位未变 | 确认开发者模式已开；地图类 App 内重启定位；部分 App 需重新打开 |
| 浏览器无法访问 | 确认后端窗口未关闭；检查端口 8765 未被占用，可用 `--port` 换端口 |
| pip 安装 pymobiledevice3 很慢/失败 | 启动器已默认走清华源；手动安装请加 `-i https://pypi.tuna.tsinghua.edu.cn/simple` |
| 启动器提示「后端启动失败并已退出」 | 多为端口 8765 被占用：关闭占用程序，或改用 `.\.venv\Scripts\python.exe backend\server.py --port 8766` 启动 |

---

## 七、安全与隐私

- 程序**仅监听本地 127.0.0.1**，不对外暴露，不上传任何数据。
- **不读取、不存储用户真实 GPS 位置**。`恢复实际定位` 会清除模拟覆盖，手机立即回到真实 GPS。
- 退出程序时会自动尝试清除定位覆盖并终止所有模拟线程。
- 请遵守当地法律法规与各 App 服务条款，勿用于欺诈或破坏他人服务。

---

## 八、许可证与免责

本项目仅供学习、开发测试与合法用途。使用者须自行承担合规责任。`pymobiledevice3` 等第三方库遵循各自许可证。
