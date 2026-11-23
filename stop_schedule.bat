@echo off
echo ==== Slack to Google Sheet: stop automatic runs ====
echo.
powershell -NoProfile -Command "Stop-ScheduledTask -TaskName 'SlackCacheSync' -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName 'SlackCacheSync' -Confirm:$false -ErrorAction Stop"
if errorlevel 1 (
    echo No scheduled task found, or it could not be removed.
) else (
    echo Done. The sync will no longer run automatically.
    echo Run install_schedule.bat any time to turn it back on.
)
echo.
pause
