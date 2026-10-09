@echo off
chcp 936 >nul
cd /d "%~dp0"
title LingNexus

set "PY=.venv\Scripts\python.exe"

rem ================================================================
rem  LingNexus 一键启动
rem    双击本文件即可。第一次会自动准备环境，以后直接启动。
rem    退出：右键右下角托盘里的 LingNexus 图标，选"退出"。
rem ================================================================

if not exist "%PY%" goto :prepare
if not exist "frontend\dist\index.html" goto :build
goto :launch

:prepare
echo [准备] 第一次运行，正在安装运行环境（需要联网，可能要几分钟）...
call :do_env
if errorlevel 1 goto :failed

:build
if not exist "frontend\dist\index.html" (
  echo [准备] 正在生成界面...
  call :do_frontend
  if errorlevel 1 goto :failed
)

:launch
echo [启动] LingNexus 正在启动，稍后浏览器会自动打开。
echo.
echo    这个黑窗口就是正在运行的程序：可以最小化，请不要直接关闭。
echo    要真正退出：右键右下角托盘里的 LingNexus 图标，选"退出"。
echo.
"%PY%" -m lingnexus.desktop --data-dir "%~dp0.ling-data"
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" exit /b 0
echo.
echo [!] 启动失败（代码 %RC%）。请把上面的文字截图发给助手。
pause
exit /b %RC%

rem ---------------- 以下是内部步骤，不用管 ----------------

:do_env
where uv >nul 2>nul
if not errorlevel 1 (
  call uv venv .venv
  if errorlevel 1 exit /b 1
  call uv sync --locked --extra dev
  if errorlevel 1 exit /b 1
  exit /b 0
)
if not exist ".venv\Scripts\uv.exe" goto :no_uv
".venv\Scripts\uv.exe" venv .venv
if errorlevel 1 exit /b 1
".venv\Scripts\uv.exe" sync --locked --extra dev
if errorlevel 1 exit /b 1
exit /b 0

:no_uv
echo [!] 缺少 Python 环境工具 uv。请先安装：https://docs.astral.sh/uv/
exit /b 1

:do_frontend
where npm >nul 2>nul
if errorlevel 1 goto :no_npm
pushd frontend
call npm ci
if errorlevel 1 goto :frontend_failed
call npm run build
if errorlevel 1 goto :frontend_failed
popd
exit /b 0

:frontend_failed
popd
exit /b 1

:no_npm
echo [!] 缺少 Node.js（用来生成界面）。请先安装：https://nodejs.org/
exit /b 1

:failed
echo.
echo [!] 准备失败。请把上面的文字截图发给助手。
pause
exit /b 1
