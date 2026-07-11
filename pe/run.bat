@echo off
chcp 65001 >nul
setlocal

REM ============================================================
REM  iPhone 定位模拟控制器 - 一键启动脚本
REM  用法:
REM    run.bat          真机模式 (需连接 iPhone)
REM    run.bat dry      模拟模式 (无需真机，体验全部 UI 流程)
REM ============================================================

title iPhone 定位模拟控制器

set "ROOT=%~dp0"
set "VENV=%ROOT%.venv"
set "PY=py -3"

echo ============================================
echo   iPhone 定位模拟控制器 启动器
echo ============================================

REM 1. 检查 Python
echo [1/4] 检查 Python ...
%PY% --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python 3。请安装 Python 3.9+ 并使用 "py" 启动器。
    echo        下载: https://www.python.org/downloads/
    pause
    exit /b 1
)

REM 2. 创建虚拟环境
if not exist "%VENV%\Scripts\python.exe" (
    echo [2/4] 创建虚拟环境 (首次较慢) ...
    %PY% -m venv "%VENV%"
    if errorlevel 1 ( echo [错误] 创建虚拟环境失败 & pause & exit /b 1 )
) else (
    echo [2/4] 虚拟环境已存在
)

set "VPY=%VENV%\Scripts\python.exe"

REM 3. 安装依赖
echo [3/4] 检查依赖 ...
"%VPY%" -c "import fastapi, uvicorn, pymobiledevice3" >nul 2>&1
if errorlevel 1 (
    echo        正在安装依赖 (首次约 1-3 分钟) ...
    "%VPY%" -m pip install --upgrade pip >nul
    "%VPY%" -m pip install -r "%ROOT%backend\requirements.txt"
    if errorlevel 1 (
        echo [错误] 依赖安装失败。请手动运行:
        echo        "%VPY%" -m pip install -r backend\requirements.txt
        pause
        exit /b 1
    )
) else (
    echo        依赖已就绪
)

REM 4. 启动服务
echo [4/4] 启动服务 ...
echo.
if /I "%1"=="dry" (
    echo   ^>^> 模拟模式 (dry-run)，无需连接真机
    echo.
    start "" http://127.0.0.1:8765
    "%VPY%" "%ROOT%backend\server.py" --dry-run --no-browser
) else (
    echo   ^>^> 真机模式。请确认 iPhone 已通过 USB 连接并信任电脑。
    echo      若首次使用未装 Apple 驱动，建议先按 README 安装 iTunes/Apple Devices。
    echo.
    "%VPY%" "%ROOT%backend\server.py"
)

endlocal
