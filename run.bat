@echo off
chcp 936 >nul 2>nul
setlocal
cd /d "%~dp0"

if not exist "%~dp0.venv\Scripts\python.exe" goto setup
"%~dp0.venv\Scripts\python.exe" -c "import PySide6" >nul 2>nul
if errorlevel 1 goto deps
goto run

:setup
echo [1/3] 首次运行，创建虚拟环境...
python -m venv .venv
if exist "%~dp0.venv\Scripts\python.exe" goto deps
rem 有些机器只有 py 启动器，没有把 python 加进 PATH
py -3 -m venv .venv
if errorlevel 1 goto nopython
if not exist "%~dp0.venv\Scripts\python.exe" goto nopython
goto deps

:deps
echo [2/3] 安装依赖，请稍候...
"%~dp0.venv\Scripts\python.exe" -m pip install -q --upgrade pip
"%~dp0.venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 goto fail
echo [3/3] 启动米家控制台...
goto run

:run
"%~dp0.venv\Scripts\python.exe" -m mihome
if errorlevel 1 pause
goto end

:nopython
echo.
echo 未找到 Python。请先安装 Python 3.10 或更高版本，安装时勾选 "Add python.exe to PATH"。
echo 下载地址：https://www.python.org/downloads/
pause
goto end

:fail
echo.
echo 依赖安装失败，请检查网络或代理设置后重试。
pause

:end
endlocal
