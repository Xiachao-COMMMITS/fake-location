@echo off
chcp 65001 >nul
title iPhone 定位模拟控制器 - 启动器
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch.ps1" %*
if errorlevel 1 pause
