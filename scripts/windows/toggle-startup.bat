@echo off
rem Switch automatic start of twitch-shorts auto mode ON / OFF (run it again to switch back).
rem Usage: toggle-startup.bat [channel]   (default: yuuki_ftw)
rem ON : creates "twitch-shorts-auto.lnk" in the Startup folder, so it starts when you sign in to Windows.
rem OFF: deletes that shortcut. An auto window that is already running keeps running until you close it.
rem To change the channel: run it once to turn OFF, then again with the new channel to turn ON.
set "TS_CHANNEL=%~1"
if "%TS_CHANNEL%"=="" set "TS_CHANNEL=yuuki_ftw"
set "TS_LAUNCHER=%~dp0start-auto.bat"
set "TS_STATE="
rem PowerShell reads the values from environment variables, so Japanese folder names and & ( ) are kept exactly.
rem Any error (locked file, unwritable folder) stops it, so a failure is never reported as ON / OFF.
for /f "usebackq delims=" %%s in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference = 'Stop'; try { $p = Join-Path ([Environment]::GetFolderPath('Startup')) 'twitch-shorts-auto.lnk'; if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p; 'OFF' } else { $s = (New-Object -ComObject WScript.Shell).CreateShortcut($p); $s.TargetPath = $env:TS_LAUNCHER; $s.Arguments = $env:TS_CHANNEL; $s.WorkingDirectory = (Split-Path $env:TS_LAUNCHER); $s.WindowStyle = 7; $s.Save(); if (-not (Test-Path -LiteralPath $p)) { throw 'shortcut was not created' }; 'ON' } } catch { [Console]::Error.WriteLine($_.Exception.Message); 'ERROR' }"`) do set "TS_STATE=%%s"
if "%TS_STATE%"=="ON" goto on
if "%TS_STATE%"=="OFF" goto off
echo Failed to switch. Please check the messages above.
pause
exit /b 1
:on
echo [ON]  twitch-shorts auto will start automatically when you sign in to Windows.
echo       Run this file again to turn it OFF.
pause
exit /b 0
:off
echo [OFF] Automatic start is turned off. Run this file again to turn it ON.
echo       If an auto window is open now, close it to stop it.
pause
exit /b 0
