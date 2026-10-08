@echo off
rem Register twitch-shorts auto mode to run when you sign in to Windows.
rem Usage: register-startup.bat [channel]   (default: yuuki_ftw)
rem To unregister, delete "twitch-shorts-auto.bat" from the Startup folder (Win+R -> shell:startup).
set "CHANNEL=%~1"
if "%CHANNEL%"=="" set "CHANNEL=yuuki_ftw"
set "LAUNCHER=%~dp0start-auto.bat"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "TARGET=%STARTUP%\twitch-shorts-auto.bat"
rem Write line by line (no parenthesized block) so paths containing ")" still work.
> "%TARGET%" echo @echo off
>> "%TARGET%" echo start "twitch-shorts" /min cmd /c ""%LAUNCHER%" %CHANNEL%"
echo Registered: "%TARGET%"
echo twitch-shorts auto %CHANNEL% will start automatically when you sign in.
pause
