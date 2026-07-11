# ============================================================
#  launch.ps1 — iPhone 定位模拟控制器 一键启动
#  做的事：准备环境 → 启动后端 → 等待就绪 → 打开前端网页 → 跟随后端生命周期
#  用法：直接运行 启动.bat（它会调用本脚本），或：
#        powershell -ExecutionPolicy Bypass -File launch.ps1
#        powershell -ExecutionPolicy Bypass -File launch.ps1 dry   # 强制 dry-run
# ============================================================

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $Root '.venv'
$Vpy  = Join-Path $Venv 'Scripts\python.exe'
$Server = Join-Path $Root 'backend\server.py'
$Host_ = '127.0.0.1'
$Port  = 8765
$Url   = "http://${Host_}:$Port"
$ForceDry = ($args -contains 'dry')

function Section($i, $msg, $color = 'Yellow') {
    Write-Host "[$i/4] $msg" -ForegroundColor $color
}
function Fail($msg) {
    Write-Host "[错误] $msg" -ForegroundColor Red
    Write-Host ""
    Read-Host "按回车退出"
    exit 1
}

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Cyan
Write-Host "    iPhone 定位模拟控制器  —  一键启动" -ForegroundColor Cyan
Write-Host "    自动启动后端服务并打开前端网页" -ForegroundColor Cyan
Write-Host "  ============================================================" -ForegroundColor Cyan
Write-Host ""

# ---------- 1. Python ----------
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    Fail "未找到 Python。请安装 Python 3.9+（安装时勾选 Add to PATH，保留 py 启动器）。`n下载: https://www.python.org/downloads/"
}
Section 1 "检测 Python ... " 'Yellow'
$pyver = (& py -3 --version 2>$null)
Write-Host "      已安装: $pyver" -ForegroundColor DarkGray

# ---------- 2. 虚拟环境 ----------
if (-not (Test-Path $Vpy)) {
    Section 2 "创建虚拟环境（首次约 10 秒）..."
    & py -3 -m venv $Venv
    if (-not (Test-Path $Vpy)) { Fail "创建虚拟环境失败。" }
} else {
    Section 2 "虚拟环境已就绪" 'Green'
}

# ---------- 3. 依赖 ----------
# 国内默认 PyPI 易超时，统一走清华镜像
$PyMirror = "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"

Section 3 "检查依赖 ..."
& $Vpy -c "import fastapi,uvicorn,pydantic" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "      安装 Web 依赖 (fastapi/uvicorn/pydantic) ... [清华源]" -ForegroundColor Yellow
    & $Vpy -m pip install -q $PyMirror fastapi "uvicorn[standard]" "pydantic>=2.6"
    if ($LASTEXITCODE -ne 0) { Fail "Web 依赖安装失败。请检查网络或换 pip 源。`n手动: $Vpy -m pip install -r backend\requirements.txt" }
} else {
    Write-Host "      Web 依赖已就绪" -ForegroundColor Green
}

$dryRun = $ForceDry
& $Vpy -c "import pymobiledevice3" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "      安装设备通信依赖 pymobiledevice3（较大,约 2-5 分钟）... [清华源]" -ForegroundColor Yellow
    & $Vpy -m pip install $PyMirror pymobiledevice3 2>&1 | Out-Null
    & $Vpy -c "import pymobiledevice3" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[警告] pymobiledevice3 安装失败 → 以 dry-run 模拟模式运行（无需真机即可体验全部流程）。" -ForegroundColor Red
        Write-Host "       真机功能请稍后手动执行: $Vpy -m pip install $PyMirror pymobiledevice3" -ForegroundColor DarkGray
        $dryRun = $true
    } else {
        Write-Host "      pymobiledevice3 已就绪" -ForegroundColor Green
    }
} else {
    Write-Host "      pymobiledevice3 已就绪" -ForegroundColor Green
}

# ---------- 4. 启动后端 + 等待就绪 + 打开网页 ----------
Section 4 "启动后端服务 ..."
# Start-Process 对含空格的路径(项目目录 "trae work")需整体引号包裹，否则 argv 被拆分导致后端退出码 2
$argStr = "`"$Server`" --no-browser --host $Host_ --port $Port"
if ($dryRun) { $argStr += ' --dry-run' }

# 在新窗口启动后端（标题提示勿关），并拿到进程对象以便跟随
$proc = Start-Process -FilePath $Vpy -ArgumentList $argStr -PassThru -WindowStyle Normal
Write-Host "      等待后端就绪 (最多 30 秒) ..." -ForegroundColor DarkGray

$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    if ($proc.HasExited) { Fail "后端启动失败并已退出（退出码 $($proc.ExitCode)）。请检查端口 $Port 是否被占用，或重试。" }
    Start-Sleep -Milliseconds 500
    try {
        $null = Invoke-RestMethod "$Url/api/health" -TimeoutSec 2
        $ready = $true; break
    } catch { }
}
if (-not $ready) {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    Fail "后端 30 秒内未就绪。可能端口被占用，换端口: 启动.bat 后无法改，请关闭占用 $Port 的程序后重试。"
}

Write-Host "      后端已就绪 ✓" -ForegroundColor Green
Write-Host "      打开前端网页 ..." -ForegroundColor Green
Start-Process $Url

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Cyan
Write-Host "    前端网页已在浏览器打开。" -ForegroundColor Cyan
if ($dryRun) {
    Write-Host "    当前为 dry-run 模拟模式（无需真机）。" -ForegroundColor Yellow
} else {
    Write-Host "    真机模式：请用 USB 连接 iPhone 并信任电脑。" -ForegroundColor Cyan
}
Write-Host "    后端服务窗口标题为 python，请勿关闭。" -ForegroundColor Cyan
Write-Host "    退出方式：关闭后端窗口，或在该窗口按 Ctrl+C。" -ForegroundColor Cyan
Write-Host "  ============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  本窗口将随后端运行保持打开；后端退出后本窗口自动关闭。" -ForegroundColor DarkGray
Write-Host ""

# 跟随后端进程：后端退出则结束
try {
    $proc.WaitForExit()
} catch {}
Write-Host "后端已结束，启动器退出。" -ForegroundColor Yellow
