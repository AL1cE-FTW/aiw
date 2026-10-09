@echo off
rem twitch-shorts auto mode launcher (Windows)
rem Usage: start-auto.bat [channel]   (default: yuuki_ftw)
chcp 65001 >nul
set PYTHONUTF8=1
pushd "%~dp0..\.."
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
set CHANNEL=%~1
if "%CHANNEL%"=="" set CHANNEL=yuuki_ftw
twitch-shorts auto %CHANNEL%
popd
pause
