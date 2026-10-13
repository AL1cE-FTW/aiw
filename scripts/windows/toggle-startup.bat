@echo off
rem Switch automatic start of twitch-shorts auto mode ON / OFF (run it again to switch back).
rem Usage: toggle-startup.bat [channel]   (default: yuuki_ftw)
rem ON : creates "twitch-shorts-auto.lnk" in the Startup folder, so it starts when you sign in to Windows.
rem OFF: deletes that shortcut. An auto window that is already running keeps running until you close it.
set "TS_CHANNEL=%~1"
if "%TS_CHANNEL%"=="" set "TS_CHANNEL=yuuki_ftw"
set "TS_LAUNCHER=%~dp0start-auto.bat"
set "TS_STATE="
rem PowerShell reads the values from environment variables, so Japanese folder names and & ( ) are kept exactly.
for /f "usebackq delims=" %%s in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = Join-Path ([Environment]::GetFolderPath('Startup')) 'twitch-shorts-auto.lnk'; if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p; 'OFF' } else { $s = (New-Object -ComObject WScript.Shell).CreateShortcut($p); $s.TargetPath = $env:TS_LAUNCHER; $s.Arguments = $env:TS_CHANNEL; $s.WorkingDirectory = (Split-Path $env:TS_LAUNCHER); $s.WindowStyle = 7; $s.Save(); 'ON' }"`) do set "TS_STATE=%%s"
if "%TS_STATE%"=="ON" (
  echo [ON]  twitch-shorts auto %TS_CHANNEL% will start automatically when you sign in to Windows.
  echo       Run this file again to turn it OFF.
) else if "%TS_STATE%"=="OFF" (
  echo [OFF] Automatic start is turned off. Run this file again to turn it ON.
  echo       If an auto window is open now, close it to stop it.
) else (
  echo Failed to switch. Please check the messages above.
  pause
  exit /b 1
)
pause
exit /b 0
