@echo off
rem Register twitch-shorts auto mode to run when you sign in to Windows.
rem Usage: register-startup.bat [channel]   (default: yuuki_ftw)
rem To unregister, delete "twitch-shorts-auto.lnk" from the Startup folder (Win+R -> shell:startup).
set "TS_CHANNEL=%~1"
if "%TS_CHANNEL%"=="" set "TS_CHANNEL=yuuki_ftw"
set "TS_LAUNCHER=%~dp0start-auto.bat"
rem A shortcut (.lnk) stores paths as Unicode, so Japanese folder names and characters such as
rem & ( ) are kept exactly. PowerShell reads the values from environment variables.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d = [Environment]::GetFolderPath('Startup'); $s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'twitch-shorts-auto.lnk')); $s.TargetPath = $env:TS_LAUNCHER; $s.Arguments = $env:TS_CHANNEL; $s.WorkingDirectory = (Split-Path $env:TS_LAUNCHER); $s.WindowStyle = 7; $s.Save(); Write-Output (Join-Path $d 'twitch-shorts-auto.lnk')"
if errorlevel 1 goto failed
echo twitch-shorts auto %TS_CHANNEL% will start automatically when you sign in.
pause
exit /b 0
:failed
echo Failed to register. Please check the messages above.
pause
exit /b 1
