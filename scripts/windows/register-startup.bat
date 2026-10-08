@echo off
rem Register twitch-shorts auto mode to run when you sign in to Windows.
rem Usage: register-startup.bat [channel]   (default: yuuki_ftw)
rem To unregister, delete "twitch-shorts-auto.bat" from the Startup folder (Win+R -> shell:startup).
set "TS_CHANNEL=%~1"
if "%TS_CHANNEL%"=="" set "TS_CHANNEL=yuuki_ftw"
set "TS_LAUNCHER=%~dp0start-auto.bat"
set "TS_TARGET=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\twitch-shorts-auto.bat"
rem PowerShell reads the paths from environment variables, so characters such as & ( ) in the
rem folder name cannot break the generated file. The launcher path is quoted exactly once.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Set-Content -LiteralPath $env:TS_TARGET -Encoding ASCII -Value @('@echo off', ('start \"twitch-shorts\" /min \"' + $env:TS_LAUNCHER + '\" ' + $env:TS_CHANNEL))"
if errorlevel 1 (
  echo Failed to register. Please check the messages above.
) else (
  echo Registered: "%TS_TARGET%"
  echo twitch-shorts auto %TS_CHANNEL% will start automatically when you sign in.
)
pause
