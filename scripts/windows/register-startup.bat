@echo off
rem Register twitch-shorts auto mode to run when you sign in to Windows.
rem Usage: register-startup.bat [channel]   (default: yuuki_ftw)
rem To unregister, delete "twitch-shorts-auto.bat" from the Startup folder (Win+R -> shell:startup).
set CHANNEL=%~1
if "%CHANNEL%"=="" set CHANNEL=yuuki_ftw
set LAUNCHER=%~dp0start-auto.bat
set STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
(
  echo @echo off
  echo start "twitch-shorts" /min cmd /c ""%LAUNCHER%" %CHANNEL%"
) > "%STARTUP%\twitch-shorts-auto.bat"
echo Registered: "%STARTUP%\twitch-shorts-auto.bat"
echo twitch-shorts auto %CHANNEL% will start automatically when you sign in.
pause
